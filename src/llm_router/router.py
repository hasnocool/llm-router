# src/llm_router/router.py
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from .async_metrics import AsyncMetricsStore
from .config import Settings, normalize_provider
from .log_events import APP_EVENT_BUFFER, attach_event_context, reset_event_context
from .metrics_db import QuotaConfig
from .metrics_report import MetricsReportGenerator
from .providers import build_provider, set_metrics_store
from .providers.base import (
    Provider,
    ProviderRequestError,
    ProviderUnavailable,
    QuotaExceededError,
    classify_request_kind,
    classify_response_kind,
)
from .schemas import ChatMessage, ChatRequest, ChatResponse, ChatUsage, Choice, ModelInfo

logger = logging.getLogger(__name__)


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
        self._bg_tasks.append(asyncio.create_task(self._drain_event_log_loop()))
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
                logging.getLogger(__name__).warning("Report generation failed: %s", exc)

    async def _drain_event_log_loop(self) -> None:
        """Flush buffered logging events (warnings, exceptions) into the event log."""
        while True:
            try:
                await asyncio.sleep(1.0)
                events = APP_EVENT_BUFFER.drain()
                if not events:
                    continue
                for event in events:
                    try:
                        await self._record_app_event(
                            level=event.get("level", "info"),
                            source=event.get("source", "app"),
                            message=event.get("message", ""),
                            provider=event.get("provider"),
                            model=event.get("model"),
                            request_id=event.get("request_id", ""),
                            details=event.get("details"),
                        )
                    except Exception:
                        logger.exception("failed to persist event log entry", extra={"skip_event_buffer": True})
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("event log drain failed: %s", exc, extra={"skip_event_buffer": True})

    async def _record_app_event(
        self,
        *,
        level: str,
        source: str,
        message: str,
        provider: str | None = None,
        model: str | None = None,
        request_id: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        try:
            await self._metrics_store.record_app_event(
                level=level,
                source=source,
                message=message,
                provider=provider,
                model=model,
                request_id=request_id,
                details=details,
            )
        except Exception:
            logger.exception("app event recording failed", extra={"skip_event_buffer": True})

    async def _log_event(
        self,
        *,
        level: str,
        source: str,
        message: str,
        provider: str | None = None,
        model: str | None = None,
        request_id: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        # Emit through the standard logging pipeline so the record reaches the
        # console/file handlers and is buffered into the dashboard event log.
        token = attach_event_context(
            provider=provider, model=model, request_id=request_id
        )
        level_no = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "critical": logging.CRITICAL,
        }.get(level.lower(), logging.INFO)
        try:
            extra: dict[str, object] = {"source": source}
            if details:
                extra["details"] = details
            logger.log(level_no, "%s", message, extra=extra)
        finally:
            reset_event_context(token)

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

    def provider_matrix_view(self) -> list[dict[str, Any]]:
        """Matrix-aware ranking view. Only meaningful under the zero-cost strategy."""
        return []

# --- routing ------------------------------------------------------

    def _routing_pool(self) -> list[str]:
        """Providers eligible for automatic/fallback routing (not explicit requests)."""
        if self.settings.routing_providers:
            return [n for n in self.settings.routing_providers if n in self.providers]
        return list(self.providers.keys())

    def _is_explicit(self, req: ChatRequest) -> bool:
        colon_provider = req.model.partition(":")[0] if ":" in req.model else None
        return bool(req.provider or normalize_provider(colon_provider) in self.providers)

    @staticmethod
    def _sse_has_tool_call(line: str) -> bool:
        if not line.startswith("data:"):
            return False
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return False
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return False
        if not isinstance(obj, dict):
            return False
        choices = obj.get("choices")
        if not isinstance(choices, list):
            return False
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict) and delta.get("tool_calls"):
                return True
        return False

    async def _record_router_event(
        self,
        *,
        provider: str | None,
        model: str,
        stream: bool,
        explicit: bool,
        failover_index: int,
        success: bool,
        task_kind: str = "general",
        request_kind: str,
        response_kind: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status_code: int | None = None,
        request_id: str = "",
    ) -> None:
        try:
            await self._metrics_store.record_router_event(
                provider=provider,
                model=model,
                task_kind=task_kind,
                stream=stream,
                explicit=explicit,
                failover_index=failover_index,
                success=success,
                request_kind=request_kind,
                response_kind=response_kind,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                status_code=status_code,
                request_id=request_id,
            )
        except Exception:
            logger.exception("router event recording failed", extra={"skip_event_buffer": True})

    @staticmethod
    def _json_body(value: object, max_chars: int = 4_000) -> str:
        try:
            raw = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raw = json.dumps(str(value), ensure_ascii=False)
        if len(raw) <= max_chars:
            return raw
        if max_chars < 2:
            return "0"

        def encoded_preview(length: int) -> str:
            return json.dumps(
                {"truncated": True, "preview": raw[:length]},
                ensure_ascii=False,
                separators=(",", ":"),
            )

        low, high = 0, min(len(raw), max_chars)
        best = "0"
        while low <= high:
            mid = (low + high) // 2
            candidate = encoded_preview(mid)
            if len(candidate) <= max_chars:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1
        return best

    def _message_log_request(self, req: ChatRequest) -> dict[str, Any]:
        return {"model": req.model, "messages": [m.model_dump(exclude_none=True) for m in req.messages]}

    async def _record_message_log(
        self,
        *,
        request_id: str,
        req: ChatRequest,
        model: str,
        stream: bool,
        explicit: bool,
        success: bool,
        request_kind: str,
        response_kind: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status_code: int | None = None,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
        provider: str | None = None,
    ) -> None:
        if not getattr(self.settings, "logs", None) or not self.settings.logs.log_message_bodies:
            return
        max_chars = self.settings.logs.max_body_chars
        error_json = ""
        try:
            if isinstance(error, ProviderRequestError) and error.body:
                error_json = error.body[:max_chars]
        except AttributeError:
            pass
        try:
            await self._metrics_store.record_message_log(
                request_id=request_id,
                provider=provider,
                model=model,
                stream=stream,
                explicit=explicit,
                success=success,
                request_kind=request_kind,
                response_kind=response_kind,
                prompt_tokens=max(0, prompt_tokens),
                completion_tokens=max(0, completion_tokens),
                latency_ms=max(0.0, latency_ms),
                status_code=status_code,
                request_json=self._json_body(self._message_log_request(req), max_chars),
                response_json=self._json_body(response or {}, max_chars) if response else "",
                error_json=error_json,
            )
        except Exception:
            logger.exception("message log recording failed", extra={"skip_event_buffer": True})

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

        # Auto-selection: "auto" (or an empty model string) picks the best-ranked
        # available provider in the routing pool using its default model, with
        # failover through the rest.
        if not req.model or req.model.lower() in {"auto", "best", "*"}:
            pool = self._routing_pool()
            ranked = self.ranked_providers()
            candidates = [n for n in pool if n in available]
            order_names = [
                s.name
                for s in ranked
                if s.name in candidates and self.settings.provider(s.name).default_model
            ]
            order_names += [
                n for n in candidates
                if n not in order_names and self.settings.provider(n).default_model
            ]
            if local_first:
                order_names = sorted(order_names, key=lambda n: (n != "local",))
            return [
                (n, self.settings.provider(n).default_model)
                for n in order_names
            ]

        primary, model = self.settings.resolve(req.model or "qwen3-8b")

        # Build fallback order: primary first, then ranked available providers.
        # The primary provider wins; if it isn't in the routing pool, only the
        # pool is used for fallback so off-limit providers never receive traffic.
        pool = self._routing_pool()
        ranked = self.ranked_providers()
        order_names = [primary]
        for s in ranked:
            if s.name != primary and s.name in available and s.name in pool:
                order_names.append(s.name)
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
        request_id = uuid.uuid4().hex[:16]
        errors: list[str] = []
        order = self._order(req)
        if not order:
            raise ProviderUnavailable("no providers configured")
        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})
        explicit = self._is_explicit(req)
        await self._log_event(
            level="info",
            source="router",
            message=f"chat request: model={req.model!r} kind={request_kind} explicit={explicit}",
            model=req.model,
            request_id=request_id,
            details={"provider_count": len(order)},
        )
        for idx, (name, model) in enumerate(order):
            provider = self.providers[name]
            t0 = time.perf_counter()
            try:
                data = await provider.complete(self._payload(req, model))
            except QuotaExceededError as exc:
                self._mark_provider_failure(name, exc)
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=False, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=429, latency_ms=(time.perf_counter() - t0) * 1000,
                )
                raise ProviderRequestError(str(exc), status_code=429) from exc
            except ProviderUnavailable as exc:
                self._mark_provider_failure(name, exc)
                errors.append(f"{name}: {exc}")
                await self._log_event(
                    level="warning",
                    source="router",
                    message=f"provider {name} unavailable: {exc}",
                    provider=name, model=model, request_id=request_id,
                    details={"status_code": exc.status_code},
                )
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=False, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=exc.status_code, latency_ms=(time.perf_counter() - t0) * 1000,
                )
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
                request_id=request_id,
            )
            await self._log_event(
                level="info",
                source="router",
                message=f"provider {name} served {model} in {latency_ms:.0f}ms",
                provider=name, model=model, request_id=request_id,
            )
            await self._record_message_log(
                request_id=request_id, req=req, model=model, stream=False, explicit=explicit,
                success=True, request_kind=request_kind,
                response_kind=classify_response_kind(data),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                latency_ms=latency_ms, status_code=200, response=data, provider=name,
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
            request_id=request_id,
        )
        await self._log_event(
            level="error",
            source="router",
            message="all providers failed: " + "; ".join(errors),
            model=req.model,
            request_id=request_id,
            details={"errors": errors},
        )
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
        request_id = uuid.uuid4().hex[:16]
        errors: list[str] = []
        order = self._order(req)
        if not order:
            raise ProviderUnavailable("no providers configured")
        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})
        explicit = self._is_explicit(req)
        await self._log_event(
            level="info",
            source="router",
            message=f"stream request: model={req.model!r} kind={request_kind} explicit={explicit}",
            model=req.model,
            request_id=request_id,
            details={"provider_count": len(order)},
        )
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
                    request_id=request_id,
                )
                await self._log_event(
                    level="info",
                    source="router",
                    message=f"provider {name} streamed {model} in {latency_ms:.0f}ms",
                    provider=name, model=model, request_id=request_id,
                )
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=True, explicit=explicit,
                    success=True, request_kind=request_kind,
                    response_kind="tool_call" if tool_response else "chat",
                    latency_ms=latency_ms, status_code=200, provider=name,
                )
                return
            except QuotaExceededError as exc:
                self._mark_provider_failure(name, exc)
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=True, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=429, latency_ms=(time.perf_counter() - t0) * 1000,
                )
                raise ProviderRequestError(str(exc), status_code=429) from exc
            except ProviderRequestError:
                raise
            except ProviderUnavailable as exc:
                self._mark_provider_failure(name, exc)
                errors.append(f"{name}: {exc}")
                await self._log_event(
                    level="warning",
                    source="router",
                    message=f"provider {name} stream unavailable: {exc}",
                    provider=name, model=model, request_id=request_id,
                    details={"status_code": exc.status_code},
                )
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=True, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=exc.status_code, latency_ms=(time.perf_counter() - t0) * 1000,
                )
                if idx < len(order) - 1:
                    continue
                await self._record_router_event(
                    provider=None,
                    model=req.model,
                    stream=True,
                    explicit=explicit,
                    failover_index=len(order),
                    success=False,
                    request_kind=request_kind,
                    status_code=exc.status_code,
                    request_id=request_id,
                )
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
        pool = self._routing_pool() or list(self.providers.keys())
        out: list[ModelInfo] = []
        seen: set[str] = set()

        # By default return the small router-defined set (aliases, provider
        # default models, and the virtual "auto" route) instead of polling
        # providers for their full remote catalogs. Model pickers stay fast
        # and unresponsive clients (e.g. OpenClaude /model) work. Pass
        # force_refresh=True (GET /v1/models?refresh=true) for full discovery.
        if not force_refresh:
            for name in pool:
                if name == "local" or not self.settings.provider(name).default_model:
                    continue
                seen.add(name)
                out.append(ModelInfo(id=name, owned_by="router"))
            for alias, route in self.settings.models.items():
                if route.provider not in pool or alias in seen:
                    continue
                seen.add(alias)
                out.append(ModelInfo(id=alias, owned_by="router"))
            if "auto" not in seen:
                seen.add("auto")
                out.append(
                    ModelInfo(id="auto", owned_by="router", created=int(time.time()))
                )
            return out

        results = await asyncio.gather(
            *(self._refresh_models_for_provider(name, force=True) for name in pool),
            return_exceptions=True,
        )
        for name, result in zip(pool, results):
            if isinstance(result, BaseException):
                continue
            for item in result:
                mid = item.get("id", "")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                out.append(
                    ModelInfo(id=f"{name}:{mid}" if name == "local" else mid, owned_by=name)
                )
        for alias, route in self.settings.models.items():
            if route.provider not in pool or alias in seen:
                continue
            seen.add(alias)
            out.append(ModelInfo(id=alias, owned_by="router"))
        # Virtual "auto" model: routes to the best-ranked available provider.
        if "auto" not in seen:
            seen.add("auto")
            out.append(
                ModelInfo(
                    id="auto",
                    owned_by="router",
                    created=int(time.time()),
                )
            )
        return out

    async def get_logs_data(
        self,
        days: int = 1,
        limit: int = 200,
        *,
        level: str | None = None,
        provider: str | None = None,
        request_id: str | None = None,
        messages_only: bool = False,
        events_only: bool = False,
    ) -> dict[str, Any]:
        since = time.time() - max(0, days) * 86400
        events: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        if not messages_only:
            levels = ("error",) if level and level == "error" else ()
            events = await self._metrics_store.get_app_events(
                limit,
                level=None if level in {None, "", "all", "error"} else level,
                levels=levels,
                provider=provider,
                request_id=request_id,
                since=since,
            )
        if not events_only:
            messages = await self._metrics_store.get_message_logs(
                limit,
                provider=provider,
                request_id=request_id,
                since=since,
            )
        return {
            "generated_at": time.time(),
            "events": events,
            "messages": messages,
            "log_level": level,
            "provider": provider,
        }

    def _pricing_for_provider(self, provider: str | None) -> tuple[float, float]:
        analytics = self.settings.analytics
        if not provider:
            return analytics.default_input_cost_per_1m_tokens, analytics.default_output_cost_per_1m_tokens
        pricing = analytics.pricing.get(provider)
        if pricing is None:
            return analytics.default_input_cost_per_1m_tokens, analytics.default_output_cost_per_1m_tokens
        return pricing.input_cost_per_1m_tokens, pricing.output_cost_per_1m_tokens

    @staticmethod
    def _as_int(value: object, default: int = 0) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float, str)):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        return default

    @staticmethod
    def _as_float(value: object, default: float = 0.0) -> float:
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float, str)):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default
        return default

    @staticmethod
    def _estimate_cost(prompt_tokens: int, completion_tokens: int, input_rate: float, output_rate: float) -> float:
        return (max(0, prompt_tokens) / 1_000_000.0) * max(0.0, input_rate) + (
            max(0, completion_tokens) / 1_000_000.0
        ) * max(0.0, output_rate)

    async def get_analytics_data(self, days: int = 30) -> dict[str, Any]:
        provider_attempts, router_attempts, app_breakdown, timeline = await asyncio.gather(
            self._metrics_store.get_provider_attempt_metrics(days),
            self._metrics_store.get_router_attempt_metrics(days),
            self._metrics_store.get_app_event_breakdown(days),
            self._metrics_store.get_request_timeline(days),
        )
        task_breakdown = await self._metrics_store.get_task_breakdown(days)

        provider_rows = {str(row.get("provider_name") or ""): row for row in provider_attempts}
        router_rows = {str(row.get("provider_name") or ""): row for row in router_attempts}
        provider_names = sorted(
            {name for name in self.providers} | {name for name in provider_rows if name} | {name for name in router_rows if name}
        )

        providers: dict[str, Any] = {}
        alerts: list[dict[str, Any]] = []
        total_attempts = 0
        total_successes = 0
        total_failures = 0
        total_failovers = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0
        total_estimated_cost = 0.0

        analytics = self.settings.analytics

        for name in provider_names:
            attempt = provider_rows.get(name, {})
            route = router_rows.get(name, {})
            attempts = self._as_int(attempt.get("attempts"))
            successes = self._as_int(attempt.get("successes"))
            failures = self._as_int(attempt.get("failures"), max(0, attempts - successes))
            failovers = self._as_int(route.get("failovers"))
            prompt_tokens = self._as_int(attempt.get("prompt_tokens"))
            completion_tokens = self._as_int(attempt.get("completion_tokens"))
            summed_tokens = self._as_int(attempt.get("total_tokens"))
            avg_latency_ms = self._as_float(attempt.get("avg_latency_ms"))
            streams = self._as_int(route.get("streams"))
            explicit_requests = self._as_int(route.get("explicit_requests"))
            route_requests = self._as_int(route.get("requests"))
            input_rate, output_rate = self._pricing_for_provider(name)
            estimated_cost = self._estimate_cost(prompt_tokens, completion_tokens, input_rate, output_rate)
            success_rate = (successes / attempts) if attempts else None
            failure_rate = (failures / attempts) if attempts else None
            failover_rate = (failovers / route_requests) if route_requests else None

            total_attempts += attempts
            total_successes += successes
            total_failures += failures
            total_failovers += failovers
            total_prompt_tokens += prompt_tokens
            total_completion_tokens += completion_tokens
            total_tokens += summed_tokens
            total_estimated_cost += estimated_cost

            providers[name] = {
                "name": name,
                "attempts": attempts,
                "successes": successes,
                "failures": failures,
                "success_rate": success_rate,
                "failure_rate": failure_rate,
                "route_requests": route_requests,
                "failovers": failovers,
                "failover_rate": failover_rate,
                "streams": streams,
                "explicit_requests": explicit_requests,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": summed_tokens,
                "avg_latency_ms": avg_latency_ms,
                "estimated_cost_usd": estimated_cost,
                "currency": analytics.currency,
            }

            if attempts >= 5 and failure_rate is not None and failure_rate >= analytics.error_rate_critical_threshold:
                alerts.append({
                    "severity": "critical",
                    "metric": "provider_failure_rate",
                    "provider": name,
                    "value": failure_rate,
                    "threshold": analytics.error_rate_critical_threshold,
                    "message": f"{name} failure rate is {failure_rate:.0%}",
                })
            elif attempts >= 5 and failure_rate is not None and failure_rate >= analytics.error_rate_warning_threshold:
                alerts.append({
                    "severity": "warning",
                    "metric": "provider_failure_rate",
                    "provider": name,
                    "value": failure_rate,
                    "threshold": analytics.error_rate_warning_threshold,
                    "message": f"{name} failure rate is {failure_rate:.0%}",
                })

            if route_requests >= 5 and failover_rate is not None and failover_rate >= analytics.failover_rate_warning_threshold:
                alerts.append({
                    "severity": "warning",
                    "metric": "provider_failover_rate",
                    "provider": name,
                    "value": failover_rate,
                    "threshold": analytics.failover_rate_warning_threshold,
                    "message": f"{name} failover rate is {failover_rate:.0%}",
                })

        overall_failover_rate = (total_failovers / max(1, sum(self._as_int(row.get("requests")) for row in router_attempts)))
        overall_success_rate = (total_successes / total_attempts) if total_attempts else None
        overall_failure_rate = (total_failures / total_attempts) if total_attempts else None
        error_levels = app_breakdown.get("levels", {})

        if overall_failure_rate is not None:
            if overall_failure_rate >= analytics.error_rate_critical_threshold:
                alerts.append({
                    "severity": "critical",
                    "metric": "overall_failure_rate",
                    "value": overall_failure_rate,
                    "threshold": analytics.error_rate_critical_threshold,
                    "message": f"Overall failure rate is {overall_failure_rate:.0%}",
                })
            elif overall_failure_rate >= analytics.error_rate_warning_threshold:
                alerts.append({
                    "severity": "warning",
                    "metric": "overall_failure_rate",
                    "value": overall_failure_rate,
                    "threshold": analytics.error_rate_warning_threshold,
                    "message": f"Overall failure rate is {overall_failure_rate:.0%}",
                })

        if overall_failover_rate >= analytics.failover_rate_warning_threshold:
            alerts.append({
                "severity": "warning",
                "metric": "overall_failover_rate",
                "value": overall_failover_rate,
                "threshold": analytics.failover_rate_warning_threshold,
                "message": f"Overall failover rate is {overall_failover_rate:.0%}",
            })

        return {
            "generated_at": time.time(),
            "days": days,
            "summary": {
                "providers": len(provider_names),
                "attempts": total_attempts,
                "successes": total_successes,
                "failures": total_failures,
                "success_rate": overall_success_rate,
                "failure_rate": overall_failure_rate,
                "failovers": total_failovers,
                "failover_rate": overall_failover_rate,
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": total_estimated_cost,
                "currency": analytics.currency,
                "error_levels": error_levels,
                "event_sources": app_breakdown.get("sources", {}),
                "task_breakdown": task_breakdown,
            },
            "timeline": timeline,
            "providers": providers,
            "alerts": alerts,
        }

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

    async def get_dashboard_data(self, days: int = 7, events: int = 50) -> dict[str, Any]:
        """Aggregate everything the dashboard renders into a single payload."""
        await self.refresh_status_from_metrics()
        daily = await asyncio.gather(
            *(self._metrics_store.get_daily_metrics(name, days) for name in self.providers)
        )
        today = await asyncio.gather(
            *(self._metrics_store.get_today_metrics(name) for name in self.providers)
        )
        quotas = await asyncio.gather(
            *(self._metrics_store.get_remaining_quota(name) for name in self.providers)
        )
        rate_limits = await asyncio.gather(
            *(self._metrics_store.get_rate_limits(name) for name in self.providers)
        )
        kind_breakdown = await self._metrics_store.get_kind_breakdown(days)
        recent_events = await self._metrics_store.get_recent_router_events(events)
        analytics = await self.get_analytics_data(days=max(1, days))
        models = await self.list_models()

        providers: dict[str, Any] = {}
        total_calls = 0
        total_failed = 0
        total_tokens = 0
        total_calls_remaining: int | None = None
        total_tokens_remaining: int | None = None
        rate_limit_warnings = 0
        for idx, name in enumerate(self.providers):
            status = self.status[name]
            today_metrics = today[idx]
            quota = quotas[idx]
            history = [
                {
                    "date": item.metric_date.isoformat(),
                    "calls": item.api_calls_total,
                    "failed": item.api_calls_failed,
                    "prompt_tokens": item.prompt_tokens,
                    "completion_tokens": item.completion_tokens,
                    "total_tokens": item.total_tokens,
                    "latency_p50_ms": item.latency_p50_ms,
                    "latency_p99_ms": item.latency_p99_ms,
                }
                for item in daily[idx]
            ]
            calls_used = today_metrics.api_calls_total if today_metrics else 0
            calls_failed = today_metrics.api_calls_failed if today_metrics else 0
            tokens_used = today_metrics.total_tokens if today_metrics else 0
            calls_remaining = quota.get("requests_remaining")
            tokens_remaining = quota.get("tokens_remaining")
            total_calls += calls_used
            total_failed += calls_failed
            total_tokens += tokens_used
            if calls_remaining is not None:
                total_calls_remaining = (total_calls_remaining or 0) + calls_remaining
            if tokens_remaining is not None:
                total_tokens_remaining = (total_tokens_remaining or 0) + tokens_remaining
            if status.rate_limit_remaining == 0:
                rate_limit_warnings += 1
            providers[name] = {
                "name": name,
                "available": status.available,
                "model_count": status.model_count,
                "last_error": status.last_error,
                "last_polled": status.last_polled,
                "daily_calls_used": calls_used,
                "daily_calls_failed": calls_failed,
                "daily_calls_remaining": calls_remaining,
                "daily_tokens_used": tokens_used,
                "daily_tokens_remaining": tokens_remaining,
                "rate_limit_remaining": status.rate_limit_remaining,
                "rate_limit_reset": status.rate_limit_reset,
                "latency_p50_ms": status.latency_p50_ms,
                "latency_p99_ms": status.latency_p99_ms,
                "rate_limits": [
                    {
                        "type": item.limit_type,
                        "limit": item.limit_value,
                        "remaining": item.remaining,
                        "reset": item.reset_timestamp,
                        "source": item.header_source,
                    }
                    for item in rate_limits[idx]
                ],
                "history": history,
            }

        matrix = self.provider_matrix_view()

        return {
            "strategy": self.settings.strategy,
            "generated_at": time.time(),
            "summary": {
                "calls_today": total_calls,
                "failed_today": total_failed,
                "success_today": total_calls - total_failed,
                "tokens_today": total_tokens,
                "providers_configured": len(self.providers),
                "providers_eligible": sum(
                    1 for item in matrix if item.get("routing", {}).get("eligible")
                ),
                "calls_remaining": total_calls_remaining,
                "tokens_remaining": total_tokens_remaining,
                "rate_limit_warnings": rate_limit_warnings,
                "kind_breakdown": kind_breakdown,
            },
            "providers": providers,
            "matrix": matrix,
            "models": [{"id": item.id, "owned_by": item.owned_by} for item in models],
            "recent_events": recent_events,
            "analytics": analytics,
        }
