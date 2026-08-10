# src/llm_router/router.py
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from .async_metrics import AsyncMetricsStore
from .config import Settings, normalize_provider
from .metrics_db import QuotaConfig
from .metrics_report import MetricsReportGenerator
from .providers import build_provider, set_metrics_store
from .providers.base import Provider, ProviderRequestError, ProviderUnavailable, QuotaExceededError
from .schemas import ChatMessage, ChatRequest, ChatResponse, ChatUsage, Choice, ModelInfo


@dataclass
class ProviderStatus:
    name: str
    available: bool = False
    model_count: int = 0
    latency_ms: float = 0.0
    last_error: str = ""
    last_polled: float = 0.0
    daily_calls_used: int = 0
    daily_calls_remaining: int | None = None
    daily_tokens_used: int = 0
    daily_tokens_remaining: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset: int | None = None
    latency_p50_ms: float = 0.0
    latency_p99_ms: float = 0.0


class ModelRouter:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self._http = http
        self.providers: dict[str, Provider] = {}
        self.status: dict[str, ProviderStatus] = {}
        self._bg_tasks: list[asyncio.Task[Any]] = []
        self._started = False

        db_path = Path(settings.metrics.db_path) if settings.metrics.db_path else Path("metrics.db")
        self._metrics_store = AsyncMetricsStore(db_path)
        set_metrics_store(self._metrics_store)

        for name, cfg in settings.providers.items():
            self.providers[name] = build_provider(name, cfg, http)
            self.status[name] = ProviderStatus(name=name)

        self._model_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._model_failures: dict[str, int] = {}
        self._model_backoff_until: dict[str, float] = {}
        self._report_path = db_path.parent / "metrics_report.md"

    async def start_background_tasks(self) -> None:
        """Start local-only maintenance tasks; no provider polling occurs here."""
        if self._started:
            return
        await self._metrics_store.start()
        for name, quota in self.settings.quotas.items():
            await self._metrics_store.upsert_quota(
                QuotaConfig(
                    provider_name=name,
                    daily_request_limit=quota.daily_request_limit,
                    daily_token_limit=quota.daily_token_limit,
                    quota_reset_hour=quota.quota_reset_hour,
                )
            )
        await self.refresh_status_from_metrics()
        retention_days = self.settings.metrics.retention_days
        report_interval = self.settings.metrics.report_interval_seconds
        self._bg_tasks.append(asyncio.create_task(self._cleanup_metrics_loop(retention_days)))
        if report_interval > 0:
            self._bg_tasks.append(asyncio.create_task(self._generate_report_loop(report_interval)))
        self._started = True

    async def stop_background_tasks(self) -> None:
        for task in self._bg_tasks:
            task.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        await self._metrics_store.close()
        self._started = False

    async def _cleanup_metrics_loop(self, retention_days: int) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
                await self._metrics_store.cleanup_old_metrics(retention_days)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("Metrics cleanup failed: %s", exc)

    async def _generate_report_loop(self, interval: int) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                providers = list(self.providers)
                report_path = self._report_path
                await self._metrics_store.run_blocking(
                    lambda db: MetricsReportGenerator(report_path, db).write_report(providers, days=7)
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning("Report generation failed: %s", exc)

    async def _enrich_status_with_metrics(self, name: str) -> None:
        today, quota_remaining, rate_limits = await asyncio.gather(
            self._metrics_store.get_today_metrics(name),
            self._metrics_store.get_remaining_quota(name),
            self._metrics_store.get_rate_limits(name),
        )
        status = self.status[name]
        if today:
            status.daily_calls_used = today.api_calls_total
            status.daily_tokens_used = today.total_tokens
            status.latency_p50_ms = today.latency_p50_ms
            status.latency_p99_ms = today.latency_p99_ms
        status.daily_calls_remaining = quota_remaining.get("requests_remaining")
        status.daily_tokens_remaining = quota_remaining.get("tokens_remaining")
        active_limits = [
            item
            for item in rate_limits
            if item.remaining is not None
            and (item.reset_timestamp is None or item.reset_timestamp > int(time.time()))
        ]
        if active_limits:
            tightest = min(active_limits, key=lambda item: item.remaining if item.remaining is not None else 0)
            status.rate_limit_remaining = tightest.remaining
            status.rate_limit_reset = tightest.reset_timestamp
        else:
            status.rate_limit_remaining = None
            status.rate_limit_reset = None

    async def refresh_status_from_metrics(self) -> dict[str, ProviderStatus]:
        """Refresh cached state without making provider network requests."""
        await asyncio.gather(*(self._enrich_status_with_metrics(name) for name in self.providers))
        return self.status

    async def poll_all_providers(self) -> dict[str, ProviderStatus]:
        """Explicit remote discovery probe, retained for diagnostics/tests only."""
        results = await asyncio.gather(
            *(self._refresh_models_for_provider(name, force=True) for name in self.providers),
            return_exceptions=True,
        )
        for name, result in zip(self.providers, results):
            if isinstance(result, BaseException):
                self._mark_provider_failure(name, result)
        await self.refresh_status_from_metrics()
        return self.status

    def _model_cache_fresh(self, name: str, now: float) -> bool:
        cached = self._model_cache.get(name)
        return bool(cached and now - cached[0] < self.settings.metrics.model_cache_ttl_seconds)

    async def _refresh_models_for_provider(
        self, name: str, *, force: bool = False
    ) -> list[dict[str, Any]]:
        now = time.time()
        if not force and self._model_cache_fresh(name, now):
            return self._model_cache[name][1]
        if not force and now < self._model_backoff_until.get(name, 0.0):
            return self._model_cache.get(name, (0.0, []))[1]

        provider = self.providers[name]
        t0 = time.perf_counter()
        try:
            models = await provider.models()
        except (ProviderUnavailable, ProviderRequestError) as exc:
            failures = self._model_failures.get(name, 0) + 1
            self._model_failures[name] = failures
            backoff = min(
                self.settings.metrics.model_failure_backoff_max_seconds,
                self.settings.metrics.model_failure_backoff_seconds * (2 ** min(failures - 1, 6)),
            )
            self._model_backoff_until[name] = now + backoff
            self._mark_provider_failure(name, exc)
            cached = self._model_cache.get(name)
            if cached:
                return cached[1]
            raise

        self._model_failures[name] = 0
        self._model_backoff_until[name] = 0.0
        self._model_cache[name] = (now, models)
        status = self.status[name]
        status.available = True
        status.model_count = len(models)
        status.latency_ms = (time.perf_counter() - t0) * 1000
        status.last_error = ""
        status.last_polled = time.time()
        return models

    def _mark_provider_success(self, name: str, latency_ms: float) -> None:
        status = self.status[name]
        status.available = True
        status.latency_ms = latency_ms
        status.last_error = ""
        status.last_polled = time.time()

    def _mark_provider_failure(self, name: str, exc: BaseException) -> None:
        status = self.status[name]
        status.last_error = str(exc)[:200]
        status.last_polled = time.time()
        if not isinstance(exc, ProviderUnavailable) or exc.status_code != 429:
            status.available = False

    def ranked_providers(self) -> list[ProviderStatus]:
        return sorted(
            self.status.values(),
            key=lambda status: (status.available, status.model_count, -status.latency_ms),
            reverse=True,
        )

    def _order(self, req: ChatRequest) -> list[tuple[str, str]]:
        local_first = req.local_first if req.local_first is not None else self.settings.strategy == "local-first"
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
        order_names = [primary]
        for status in self.ranked_providers():
            if status.name != primary and status.name in available:
                order_names.append(status.name)
        if local_first:
            order_names = sorted(order_names, key=lambda name: name != "local")
        return [
            (name, model if name == primary else self.settings.provider(name).default_model)
            for name in order_names
            if name in available
        ]

    @staticmethod
    def _payload(req: ChatRequest, model: str) -> dict[str, Any]:
        payload = req.model_dump(exclude={"provider", "local_first"}, exclude_none=True)
        payload["model"] = model
        return payload

    async def complete(self, req: ChatRequest) -> ChatResponse:
        errors: list[str] = []
        order = self._order(req)
        if not order:
            raise ProviderUnavailable("no providers configured")
        for name, model in order:
            provider = self.providers[name]
            t0 = time.perf_counter()
            try:
                data = await provider.complete(self._payload(req, model))
            except QuotaExceededError as exc:
                self._mark_provider_failure(name, exc)
                raise ProviderRequestError(str(exc), status_code=429) from exc
            except ProviderUnavailable as exc:
                self._mark_provider_failure(name, exc)
                errors.append(f"{name}: {exc}")
                continue
            self._mark_provider_success(name, (time.perf_counter() - t0) * 1000)
            await self._enrich_status_with_metrics(name)
            return self._to_response(data, model, provider_name=name)
        raise ProviderUnavailable("all providers failed: " + "; ".join(errors))

    def _to_response(self, data: dict[str, Any], model: str, provider_name: str) -> ChatResponse:
        choice = data["choices"][0]
        msg = choice.get("message") or {"role": "assistant", "content": ""}
        usage = data.get("usage")
        raw_tool_calls = msg.get("tool_calls")
        tool_calls = raw_tool_calls if isinstance(raw_tool_calls, list) else None
        return ChatResponse(
            id=data.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}",
            created=data.get("created") or int(time.time()),
            model=model,
            choices=[Choice(
                index=choice.get("index", 0),
                message=ChatMessage(
                    role=msg.get("role", "assistant"),
                    content=msg.get("content"),
                    reasoning_content=msg.get("reasoning_content"),
                    tool_calls=tool_calls,
                ),
                finish_reason=choice.get("finish_reason", "stop"),
            )],
            usage=(ChatUsage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ) if isinstance(usage, dict) else None),
            provider=provider_name,
        )

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        errors: list[str] = []
        order = self._order(req)
        if not order:
            raise ProviderUnavailable("no providers configured")
        for idx, (name, model) in enumerate(order):
            provider = self.providers[name]
            t0 = time.perf_counter()
            try:
                async for line in provider.stream(self._payload(req, model)):
                    yield self._annotate_sse(line, name)
                self._mark_provider_success(name, (time.perf_counter() - t0) * 1000)
                await self._enrich_status_with_metrics(name)
                return
            except QuotaExceededError as exc:
                self._mark_provider_failure(name, exc)
                raise ProviderRequestError(str(exc), status_code=429) from exc
            except ProviderRequestError:
                raise
            except ProviderUnavailable as exc:
                self._mark_provider_failure(name, exc)
                errors.append(f"{name}: {exc}")
                if idx < len(order) - 1:
                    continue
                raise ProviderUnavailable("all providers failed: " + "; ".join(errors)) from exc

    @staticmethod
    def _annotate_sse(line: str, provider_name: str) -> str:
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

    async def list_models(self, *, force_refresh: bool = False) -> list[ModelInfo]:
        results = await asyncio.gather(
            *(self._refresh_models_for_provider(name, force=force_refresh) for name in self.providers),
            return_exceptions=True,
        )
        out: list[ModelInfo] = []
        seen_routes: set[tuple[str, str]] = set()
        for name, result in zip(self.providers, results):
            if isinstance(result, BaseException):
                continue
            for item in result:
                model_id = item.get("id", "")
                if not model_id or (name, model_id) in seen_routes:
                    continue
                seen_routes.add((name, model_id))
                out.append(ModelInfo(id=f"{name}:{model_id}", owned_by=name))
        for alias in self.settings.models:
            out.append(ModelInfo(id=alias, owned_by="router"))
        return out

    async def get_provider_metrics(self, provider: str, days: int = 7) -> dict[str, Any]:
        daily, today, quota_remaining, rate_limits = await asyncio.gather(
            self._metrics_store.get_daily_metrics(provider, days),
            self._metrics_store.get_today_metrics(provider),
            self._metrics_store.get_remaining_quota(provider),
            self._metrics_store.get_rate_limits(provider),
        )
        active = [
            item for item in rate_limits
            if item.remaining is not None
            and (item.reset_timestamp is None or item.reset_timestamp > int(time.time()))
        ]
        tightest = min(
            active,
            key=lambda item: item.remaining if item.remaining is not None else 0,
        ) if active else None
        return {
            "provider": provider,
            "daily_calls_used": today.api_calls_total if today else 0,
            "daily_calls_remaining": quota_remaining.get("requests_remaining"),
            "daily_tokens_used": today.total_tokens if today else 0,
            "daily_tokens_remaining": quota_remaining.get("tokens_remaining"),
            "rate_limit_remaining": tightest.remaining if tightest else None,
            "rate_limit_reset": tightest.reset_timestamp if tightest else None,
            "rate_limit_type": tightest.limit_type if tightest else None,
            "rate_limits": [{
                "type": item.limit_type,
                "limit": item.limit_value,
                "remaining": item.remaining,
                "reset": item.reset_timestamp,
                "source": item.header_source,
            } for item in rate_limits],
            "latency_p50_ms": today.latency_p50_ms if today else 0.0,
            "latency_p99_ms": today.latency_p99_ms if today else 0.0,
            "history": [{
                "date": item.metric_date.isoformat(),
                "calls": item.api_calls_total,
                "failed": item.api_calls_failed,
                "prompt_tokens": item.prompt_tokens,
                "completion_tokens": item.completion_tokens,
                "total_tokens": item.total_tokens,
                "latency_p50_ms": item.latency_p50_ms,
                "latency_p99_ms": item.latency_p99_ms,
            } for item in daily],
        }

    async def get_all_metrics(self, days: int = 7) -> dict[str, Any]:
        values = await asyncio.gather(
            *(self.get_provider_metrics(provider, days) for provider in self.providers)
        )
        return dict(zip(self.providers, values))
