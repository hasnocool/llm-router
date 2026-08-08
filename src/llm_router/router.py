from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .config import Settings, normalize_provider
from .providers import build_provider
from .providers.base import ProviderRequestError, ProviderUnavailable
from .schemas import ChatMessage, ChatRequest, ChatResponse, ChatUsage, Choice, ModelInfo


@dataclass
class ProviderStatus:
    name: str
    available: bool = False
    model_count: int = 0
    latency_ms: float = 0.0
    last_error: str = ""
    last_polled: float = 0.0


class ModelRouter:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self._http = http
        self.providers: dict[str, object] = {}
        self.status: dict[str, ProviderStatus] = {}
        for name, cfg in settings.providers.items():
            self.providers[name] = build_provider(name, cfg, http)
            self.status[name] = ProviderStatus(name=name)

    # --- availability polling ------------------------------------------

    async def poll_all_providers(self) -> dict[str, ProviderStatus]:
        """Poll each provider's models endpoint concurrently. Update status."""
        now = time.time()
        results = await asyncio.gather(
            *(self._poll_one(name) for name in self.providers),
            return_exceptions=True,
        )
        for name, result in zip(self.providers, results):
            if isinstance(result, Exception):
                self.status[name].available = False
                self.status[name].last_error = str(result)
                self.status[name].last_polled = now
            else:
                self.status[name] = result
        return self.status

    async def _poll_one(self, name: str) -> ProviderStatus:
        provider = self.providers[name]
        s = ProviderStatus(name=name)
        t0 = time.time()
        try:
            data = await provider.models()
            s.available = True
            s.model_count = len(data)
        except ProviderRequestError as exc:
            s.available = False
            s.last_error = f"HTTP {exc.status_code}: {exc.body[:120]}"
        except ProviderUnavailable as exc:
            s.available = False
            s.last_error = str(exc)
        except Exception as exc:
            s.available = False
            s.last_error = str(exc)[:200]
        s.latency_ms = (time.time() - t0) * 1000
        s.last_polled = time.time()
        return s

    def ranked_providers(self) -> list[ProviderStatus]:
        """Return providers sorted by availability then model count (descending)."""
        return sorted(
            self.status.values(),
            key=lambda s: (s.available, s.model_count),
            reverse=True,
        )

    # --- routing ------------------------------------------------------

    def _order(self, req: ChatRequest) -> list[tuple[str, str]]:
        """Return ordered [(provider_name, model_id)] to attempt."""
        local_first = (
            req.local_first
            if req.local_first is not None
            else self.settings.strategy == "local-first"
        )
        available = list(self.providers.keys())

        colon_provider = req.model.partition(":")[0] if ":" in req.model else None
        explicit = req.provider or (
            colon_provider if normalize_provider(colon_provider) in self.providers else None
        )
        if explicit:
            name = normalize_provider(explicit)
            if name not in self.providers:
                raise ProviderRequestError(f"unknown provider: {name}", status_code=400)
            if ":" in req.model:
                model = req.model.partition(":")[2]
            else:
                _, model = self.settings.resolve(req.model or "")
            if not model:
                model = self.settings.provider(name).default_model
            return [(name, model)]

        primary, model = self.settings.resolve(req.model or "qwen3-8b")

        # Build fallback order: primary first, then ranked available providers
        ranked = self.ranked_providers()
        order_names = [primary]
        for s in ranked:
            if s.name != primary and s.name in available:
                order_names.append(s.name)
        if local_first:
            order_names = sorted(order_names, key=lambda n: (n != "local",))
        return [
            (n, model if n == primary else self.settings.provider(n).default_model)
            for n in order_names if n in available
        ]

    def _payload(self, req: ChatRequest, model: str) -> dict:
        payload = req.model_dump(exclude={"provider", "local_first"}, exclude_none=True)
        payload["model"] = model
        return payload

    # --- non-streaming ------------------------------------------------

    async def complete(self, req: ChatRequest) -> ChatResponse:
        errors: list[str] = []
        order = self._order(req)
        if not order:
            raise ProviderUnavailable("no providers configured")
        for name, model in order:
            provider = self.providers[name]
            try:
                data = await provider.complete(self._payload(req, model))
            except ProviderUnavailable as exc:
                errors.append(f"{name}: {exc}")
                continue
            return self._to_response(data, model, provider_name=name)
        raise ProviderUnavailable("all providers failed: " + "; ".join(errors))

    def _to_response(self, data: dict, model: str, provider_name: str) -> ChatResponse:
        choice = data["choices"][0]
        msg = choice.get("message") or {"role": "assistant", "content": ""}
        usage = data.get("usage")
        return ChatResponse(
            id=data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}",
            created=data.get("created") or int(time.time()),
            model=model,
            choices=[
                Choice(
                    index=choice.get("index", 0),
                    message=ChatMessage(
                        role=msg.get("role", "assistant"),
                        content=msg.get("content") or "",
                        reasoning_content=msg.get("reasoning_content"),
                    ),
                    finish_reason=choice.get("finish_reason", "stop"),
                )
            ],
            usage=(
                ChatUsage(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                )
                if usage
                else None
            ),
            provider=provider_name,
        )

    # --- streaming ----------------------------------------------------

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        """Yields raw SSE lines for the first working provider."""
        errors: list[str] = []
        order = self._order(req)
        if not order:
            raise ProviderUnavailable("no providers configured")
        for idx, (name, model) in enumerate(order):
            provider = self.providers[name]
            try:
                async for line in provider.stream(self._payload(req, model)):
                    yield self._annotate_sse(line, name)
                return
            except ProviderRequestError as exc:
                raise
            except ProviderUnavailable as exc:
                errors.append(f"{name}: {exc}")
                if idx < len(order) - 1:
                    continue
                raise ProviderUnavailable(
                    "all providers failed: " + "; ".join(errors)
                ) from exc

    def _annotate_sse(self, line: str, provider_name: str) -> str:
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    obj = json.loads(payload)
                    obj["provider"] = provider_name
                    return "data: " + json.dumps(obj)
                except json.JSONDecodeError:
                    return line
        return line

    # --- model discovery ----------------------------------------------

    async def list_models(self) -> list[ModelInfo]:
        out: list[ModelInfo] = []
        seen: set[str] = set()
        for name in self.providers:
            provider = self.providers[name]
            try:
                data = await provider.models()
            except (ProviderUnavailable, ProviderRequestError):
                continue
            for item in data:
                mid = item.get("id", "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                out.append(
                    ModelInfo(id=f"{name}:{mid}" if name == "local" else mid, owned_by=name)
                )
        for alias in self.settings.models:
            if alias not in seen:
                seen.add(alias)
                out.append(ModelInfo(id=alias, owned_by="router"))
        return out
