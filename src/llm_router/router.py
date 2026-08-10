from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from .config import Settings, normalize_provider
from .metrics_db import get_metrics_db
from .metrics_report import MetricsReportGenerator
from .providers import build_provider, set_metrics_db
from .providers.base import ProviderRequestError, ProviderUnavailable, QuotaExceededError
from .schemas import ChatMessage, ChatRequest, ChatResponse, ChatUsage, Choice, ModelInfo


@dataclass
class ProviderStatus:
    name: str
    available: bool = False
    model_count: int = 0
    latency_ms: float = 0.0
    last_error: str = ""
    last_polled: float = 0.0
    # Metrics fields
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
        self.providers: dict[str, object] = {}
        self.status: dict[str, ProviderStatus] = {}
        self._bg_tasks: list[asyncio.Task] = []

        # Initialize metrics DB
        from pathlib import Path
        db_path = None
        if settings.metrics.db_path:
            db_path = Path(settings.metrics.db_path)
        self._metrics_db = get_metrics_db(db_path=db_path)
        set_metrics_db(self._metrics_db)

        # Load quotas into metrics DB
        for name, quota in settings.quotas.items():
            from .metrics_db import QuotaConfig
            self._metrics_db.upsert_quota(QuotaConfig(
                provider_name=name,
                daily_request_limit=quota.daily_request_limit,
                daily_token_limit=quota.daily_token_limit,
                quota_reset_hour=quota.quota_reset_hour,
            ))

        for name, cfg in settings.providers.items():
            self.providers[name] = build_provider(name, cfg, http)
            self.status[name] = ProviderStatus(name=name)

        # Initialize metrics report generator
        self._report_generator = MetricsReportGenerator(
            report_path=(Path(settings.metrics.db_path).parent / "metrics_report.md") if settings.metrics.db_path else Path("metrics_report.md"),
            config_path=(Path(settings.metrics.db_path).parent / "config.toml") if settings.metrics.db_path else None,
        )

    def start_background_tasks(self):
        """Start background tasks for metrics polling and cleanup."""
        flush_interval = self.settings.metrics.flush_interval_seconds
        retention_days = self.settings.metrics.retention_days
        report_interval = getattr(self.settings.metrics, 'report_interval_seconds', 300)  # Default 5 min
        
        self._bg_tasks.append(asyncio.create_task(self._poll_rate_limits_loop()))
        self._bg_tasks.append(asyncio.create_task(self._cleanup_metrics_loop(retention_days)))
        self._bg_tasks.append(asyncio.create_task(self._flush_metrics_loop(flush_interval)))
        self._bg_tasks.append(asyncio.create_task(self._generate_report_loop(report_interval)))

    async def stop_background_tasks(self):
        """Stop all background tasks."""
        for task in self._bg_tasks:
            task.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

    async def _poll_rate_limits_loop(self):
        """Periodically poll provider models endpoint to refresh rate limit headers."""
        while True:
            try:
                await asyncio.sleep(60)  # Poll every 60 seconds
                await self.poll_all_providers()
            except asyncio.CancelledError:
                break
            except BaseException as e:
                # Log but continue - don't crash the server
                import logging
                logging.getLogger(__name__).warning(f"Rate limit poll failed: {e}")

    async def _cleanup_metrics_loop(self, retention_days: int):
        """Periodically clean up old metrics data."""
        while True:
            try:
                await asyncio.sleep(3600)  # Run every hour
                self._metrics_db.cleanup_old_metrics(retention_days)
            except asyncio.CancelledError:
                break
            except BaseException as e:
                import logging
                logging.getLogger(__name__).warning(f"Metrics cleanup failed: {e}")

    async def _flush_metrics_loop(self, interval: int):
        """Periodically ensure metrics are flushed (SQLite auto-commits, but good for explicit flush)."""
        while True:
            try:
                await asyncio.sleep(interval)
                # SQLite commits on each transaction, but we can add explicit flush if needed
            except asyncio.CancelledError:
                break
            except BaseException as e:
                import logging
                logging.getLogger(__name__).warning(f"Metrics flush failed: {e}")

    async def _generate_report_loop(self, interval: int):
        """Periodically generate markdown metrics report."""
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Report generation loop started with interval {interval}s")
        while True:
            try:
                await asyncio.sleep(interval)
                logger.info("Generating metrics report...")
                self._report_generator.write_report(days=7)
                logger.info("Metrics report generated successfully")
            except asyncio.CancelledError:
                break
            except BaseException as e:
                logger.warning(f"Report generation failed: {e}")

    def _enrich_status_with_metrics(self, name: str) -> None:
        """Add metrics data to provider status."""
        today = self._metrics_db.get_today_metrics(name)
        quota_remaining = self._metrics_db.get_remaining_quota(name)
        rate_limit = self._metrics_db.get_rate_limit(name)

        status = self.status[name]
        if today:
            status.daily_calls_used = today.api_calls_total
            status.daily_tokens_used = today.total_tokens
            status.latency_p50_ms = today.latency_p50_ms
            status.latency_p99_ms = today.latency_p99_ms
        status.daily_calls_remaining = quota_remaining.get("requests_remaining")
        status.daily_tokens_remaining = quota_remaining.get("tokens_remaining")
        if rate_limit:
            status.rate_limit_remaining = rate_limit.remaining
            status.rate_limit_reset = rate_limit.reset_timestamp

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
            # Enrich with metrics
            self._enrich_status_with_metrics(name)
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

    def _routing_pool(self) -> list[str]:
        """Providers eligible for automatic/fallback routing (not explicit requests)."""
        if self.settings.routing_providers:
            return [n for n in self.settings.routing_providers if n in self.providers]
        return list(self.providers.keys())

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
            except QuotaExceededError as exc:
                # Quota exceeded - don't failover, return error directly
                raise ProviderRequestError(str(exc), status_code=429) from exc
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
            except QuotaExceededError as exc:
                # Quota exceeded - don't failover, return error directly
                raise ProviderRequestError(str(exc), status_code=429) from exc
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
        pool = self._routing_pool() or list(self.providers.keys())
        for name in pool:
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

    # --- metrics endpoints --------------------------------------------

    def get_provider_metrics(self, provider: str, days: int = 7) -> dict[str, Any]:
        """Get detailed metrics for a specific provider."""
        daily = self._metrics_db.get_daily_metrics(provider, days)
        today = self._metrics_db.get_today_metrics(provider)
        quota_remaining = self._metrics_db.get_remaining_quota(provider)
        rate_limit = self._metrics_db.get_rate_limit(provider)

        return {
            "provider": provider,
            "daily_calls_used": today.api_calls_total if today else 0,
            "daily_calls_remaining": quota_remaining.get("requests_remaining"),
            "daily_tokens_used": today.total_tokens if today else 0,
            "daily_tokens_remaining": quota_remaining.get("tokens_remaining"),
            "rate_limit_remaining": rate_limit.remaining if rate_limit else None,
            "rate_limit_reset": rate_limit.reset_timestamp if rate_limit else None,
            "rate_limit_type": rate_limit.limit_type if rate_limit else None,
            "latency_p50_ms": today.latency_p50_ms if today else 0,
            "latency_p99_ms": today.latency_p99_ms if today else 0,
            "history": [
                {
                    "date": d.metric_date.isoformat(),
                    "calls": d.api_calls_total,
                    "failed": d.api_calls_failed,
                    "prompt_tokens": d.prompt_tokens,
                    "completion_tokens": d.completion_tokens,
                    "total_tokens": d.total_tokens,
                    "latency_p50_ms": d.latency_p50_ms,
                    "latency_p99_ms": d.latency_p99_ms,
                }
                for d in daily
            ],
        }

    def get_all_metrics(self, days: int = 7) -> dict[str, Any]:
        """Get metrics for all providers."""
        return {
            provider: self.get_provider_metrics(provider, days)
            for provider in self.providers
        }