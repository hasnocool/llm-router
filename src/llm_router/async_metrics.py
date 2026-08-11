# src/llm_router/async_metrics.py
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from .metrics_db import MetricsDB, QuotaConfig

T = TypeVar("T")


class AsyncMetricsStore:
    """Single-worker async facade for blocking SQLite operations."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="llm-router-metrics")
        self._db: MetricsDB | None = None
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._db is not None:
            return
        async with self._start_lock:
            if self._db is None:
                loop = asyncio.get_running_loop()
                self._db = await loop.run_in_executor(self._executor, MetricsDB, self.db_path)

    async def _call(self, func: Callable[[MetricsDB], T]) -> T:
        await self.start()
        assert self._db is not None
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(func, self._db))

    async def upsert_quota(self, quota: QuotaConfig) -> None:
        await self._call(lambda db: db.upsert_quota(quota))

    async def reserve_quota(
        self, provider: str, estimated_tokens: int = 0, ttl_seconds: int = 600
    ) -> str | None:
        return await self._call(
            lambda db: db.reserve_quota(provider, estimated_tokens, ttl_seconds)
        )

    async def reconcile_reservation(
        self,
        reservation_id: str | None,
        provider: str,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status_code: int | None = None,
        request_kind: str = "chat",
        response_kind: str = "",
    ) -> None:
        await self._call(
            lambda db: db.reconcile_reservation(
                reservation_id,
                provider,
                success,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                status_code,
                request_kind,
                response_kind,
            )
        )

    async def record_router_event(
        self,
        provider: str | None,
        model: str,
        stream: bool,
        explicit: bool,
        failover_index: int,
        success: bool,
        request_kind: str = "chat",
        response_kind: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status_code: int | None = None,
        occurred_at: float | None = None,
    ) -> None:
        await self._call(
            lambda db: db.record_router_event(
                provider,
                model,
                stream,
                explicit,
                failover_index,
                success,
                request_kind,
                response_kind,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                status_code,
                occurred_at,
            )
        )

    async def get_recent_router_events(self, limit: int = 100):
        return await self._call(lambda db: db.get_recent_router_events(limit))

    async def get_kind_breakdown(self, days: int = 7):
        return await self._call(lambda db: db.get_kind_breakdown(days))

    async def cancel_reservation(self, reservation_id: str | None) -> None:
        await self._call(lambda db: db.cancel_reservation(reservation_id))

    async def record_request(self, **kwargs: Any) -> None:
        await self._call(lambda db: db.record_request(**kwargs))

    async def upsert_rate_limits(self, provider: str, limits: Sequence[object]) -> None:
        if not limits:
            return
        await self._call(lambda db: db.upsert_rate_limits(provider, limits))

    async def get_rate_limits(self, provider: str):
        return await self._call(lambda db: db.get_rate_limits(provider))

    async def get_rate_limit(self, provider: str, limit_type: str | None = None):
        return await self._call(lambda db: db.get_rate_limit(provider, limit_type))

    async def get_all_rate_limits(self):
        return await self._call(lambda db: db.get_all_rate_limits())

    async def get_quota(self, provider: str):
        return await self._call(lambda db: db.get_quota(provider))

    async def get_all_quotas(self):
        return await self._call(lambda db: db.get_all_quotas())

    async def check_quota_exceeded(self, provider: str):
        return await self._call(lambda db: db.check_quota_exceeded(provider))

    async def get_remaining_quota(self, provider: str):
        return await self._call(lambda db: db.get_remaining_quota(provider))

    async def get_today_metrics(self, provider: str):
        return await self._call(lambda db: db.get_today_metrics(provider))

    async def get_daily_metrics(self, provider: str, days: int = 7):
        return await self._call(lambda db: db.get_daily_metrics(provider, days))

    async def cleanup_old_metrics(self, retention_days: int = 30) -> None:
        await self._call(lambda db: db.cleanup_old_metrics(retention_days))

    async def run_blocking(self, func: Callable[[MetricsDB], T]) -> T:
        return await self._call(func)

    async def close(self) -> None:
        if self._db is not None:
            await self._call(lambda db: db.close())
            self._db = None
        self._executor.shutdown(wait=True, cancel_futures=True)
