# src/llm_router/zero_cost_router.py
from __future__ import annotations

import time
from typing import AsyncIterator

import httpx

from .config import Settings, normalize_provider
from .provider_matrix import get_provider_matrix_entry, load_provider_matrix
from .providers.base import (
    ProviderRequestError,
    ProviderUnavailable,
    QuotaExceededError,
    classify_request_kind,
    classify_response_kind,
)
from .router import ModelRouter, ProviderStatus
from .routing_score import RouteScore, ZeroCostPolicy, route_sort_key, runtime_route_score
from .schemas import ChatRequest, ChatResponse


class ZeroCostModelRouter(ModelRouter):
    """ModelRouter variant that drains renewable free routes before paid/finite routes."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        policy: ZeroCostPolicy | None = None,
    ) -> None:
        load_provider_matrix()
        super().__init__(settings, http)
        self.zero_cost_policy = policy or ZeroCostPolicy.from_env()

    def score_provider(self, name: str, *, primary: bool = False) -> RouteScore:
        return runtime_route_score(
            name,
            get_provider_matrix_entry(name),
            self.status.get(name),
            self.zero_cost_policy,
            primary=primary,
        )

    def route_scores(self, *, primary: str | None = None) -> list[RouteScore]:
        scores = [self.score_provider(name, primary=(name == primary)) for name in self.providers]
        return sorted(scores, key=route_sort_key)

    def ranked_providers(self) -> list[ProviderStatus]:
        if self.settings.strategy != "zero-cost":
            return super().ranked_providers()
        scores = self.route_scores()
        order = {score.provider: idx for idx, score in enumerate(scores) if score.eligible}
        return sorted(
            self.status.values(),
            key=lambda status: order.get(status.name, len(order) + 1_000),
        )

    def _order(self, req: ChatRequest) -> list[tuple[str, str]]:
        if self.settings.strategy != "zero-cost":
            return super()._order(req)

        colon_provider = req.model.partition(":")[0] if ":" in req.model else None
        explicit = req.provider or (
            colon_provider if normalize_provider(colon_provider) in self.providers else None
        )
        if explicit:
            return super()._order(req)

        primary, primary_model = self.settings.resolve(req.model or "qwen3-8b")

        # "auto" (or an empty model string) picks the best-ranked eligible
        # provider in the routing pool using its default model, with failover.
        if not req.model or req.model.lower() in {"auto", "best", "*"}:
            pool = self._routing_pool()
            scores = [
                score for score in self.route_scores()
                if score.provider in pool
                and self.settings.provider(score.provider).default_model
            ]
            names = [score.provider for score in scores]
            if req.local_first is True and "local" in names:
                names.remove("local")
                names.insert(0, "local")
            elif req.local_first is False and "local" in names:
                names = [name for name in names if name != "local"] + ["local"]
            return [
                (name, self.settings.provider(name).default_model)
                for name in names
            ]

        scores = [score for score in self.route_scores(primary=primary) if score.eligible]
        names = [score.provider for score in scores]

        if req.local_first is True and "local" in names:
            names.remove("local")
            names.insert(0, "local")
        elif req.local_first is False and "local" in names:
            names = [name for name in names if name != "local"] + ["local"]

        return [
            (
                name,
                primary_model if name == primary else self.settings.provider(name).default_model,
            )
            for name in names
        ]

    async def complete(self, req: ChatRequest) -> ChatResponse:
        if self.settings.strategy != "zero-cost":
            return await super().complete(req)

        errors: list[str] = []
        order = self._order(req)
        if not order:
            raise ProviderUnavailable("no zero-cost providers are currently eligible")

        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})
        explicit = self._is_explicit(req)
        for idx, (name, model) in enumerate(order):
            provider = self.providers[name]
            t0 = time.perf_counter()
            try:
                data = await provider.complete(self._payload(req, model))
            except QuotaExceededError as exc:
                errors.append(f"{name}: {exc}")
                self._mark_quota_exhausted(name)
                continue
            except ProviderUnavailable as exc:
                errors.append(f"{name}: {exc}")
                self._mark_retryable_failure(name, exc)
                continue
            except ProviderRequestError as exc:
                if len(order) == 1:
                    raise
                errors.append(f"{name}: {exc}")
                self._mark_provider_failure(name, exc)
                continue
            latency_ms = (time.perf_counter() - t0) * 1000
            self._mark_provider_success(name, latency_ms)
            await self._enrich_status_with_metrics(name)
            raw_usage = data.get("usage")
            usage = raw_usage if isinstance(raw_usage, dict) else {}
            await self._record_router_event(
                provider=name,
                model=model,
                stream=False,
                explicit=explicit,
                failover_index=idx,
                success=True,
                request_kind=request_kind,
                response_kind=classify_response_kind(data),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                latency_ms=latency_ms,
                status_code=200,
            )
            return self._to_response(data, model, provider_name=name)

        await self._record_router_event(
            provider=None,
            model=req.model,
            stream=False,
            explicit=explicit,
            failover_index=len(order),
            success=False,
            request_kind=request_kind,
            status_code=None,
        )
        raise ProviderUnavailable("all zero-cost providers failed: " + "; ".join(errors))

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        if self.settings.strategy != "zero-cost":
            async for line in super().stream(req):
                yield line
            return

        errors: list[str] = []
        order = self._order(req)
        if not order:
            raise ProviderUnavailable("no zero-cost providers are currently eligible")

        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})
        explicit = self._is_explicit(req)
        for idx, (name, model) in enumerate(order):
            provider = self.providers[name]
            t0 = time.perf_counter()
            tool_response = False
            try:
                async for line in provider.stream(self._payload(req, model)):
                    if self._sse_has_tool_call(line):
                        tool_response = True
                    yield self._annotate_sse(line, name)
                latency_ms = (time.perf_counter() - t0) * 1000
                self._mark_provider_success(name, latency_ms)
                await self._enrich_status_with_metrics(name)
                await self._record_router_event(
                    provider=name,
                    model=model,
                    stream=True,
                    explicit=explicit,
                    failover_index=idx,
                    success=True,
                    request_kind=request_kind,
                    response_kind="tool_call" if tool_response else "chat",
                    latency_ms=latency_ms,
                    status_code=200,
                )
                return
            except QuotaExceededError as exc:
                errors.append(f"{name}: {exc}")
                self._mark_quota_exhausted(name)
                continue
            except ProviderUnavailable as exc:
                errors.append(f"{name}: {exc}")
                self._mark_retryable_failure(name, exc)
                continue
            except ProviderRequestError as exc:
                if len(order) == 1:
                    raise
                errors.append(f"{name}: {exc}")
                self._mark_provider_failure(name, exc)
                continue

        await self._record_router_event(
            provider=None,
            model=req.model,
            stream=True,
            explicit=explicit,
            failover_index=len(order),
            success=False,
            request_kind=request_kind,
            status_code=None,
        )
        raise ProviderUnavailable("all zero-cost providers failed: " + "; ".join(errors))

    def _mark_quota_exhausted(self, name: str) -> None:
        status = self.status.get(name)
        if status is None:
            return
        status.daily_calls_remaining = 0
        status.last_error = "local quota guard exhausted"
        status.last_polled = time.time()

    def _mark_retryable_failure(self, name: str, exc: ProviderUnavailable) -> None:
        status = self.status.get(name)
        if status is None:
            return
        status.last_error = str(exc)[:200]
        status.last_polled = time.time()
        if exc.status_code == 429:
            status.rate_limit_remaining = 0
            status.rate_limit_reset = exc.retry_after_until
        else:
            status.available = False

    def provider_matrix_view(self) -> list[dict]:
        scores = {score.provider: score for score in self.route_scores()}
        output: list[dict] = []
        for name in self.providers:
            entry = get_provider_matrix_entry(name)
            score = scores[name]
            item = entry.as_public_dict() if entry is not None else {"id": None, "name": name}
            item["configured_provider"] = name
            item["routing"] = score.as_dict()
            output.append(item)
        output.sort(key=lambda item: route_sort_key(scores[item["configured_provider"]]))
        return output
