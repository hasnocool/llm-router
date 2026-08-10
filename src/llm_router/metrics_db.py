from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


SCHEMA = """
-- Daily aggregated metrics per provider
CREATE TABLE IF NOT EXISTS provider_daily_metrics (
    provider_name TEXT NOT NULL,
    metric_date DATE NOT NULL,
    api_calls_total INTEGER DEFAULT 0,
    api_calls_failed INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    latency_sum_ms REAL DEFAULT 0,
    latency_count INTEGER DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider_name, metric_date)
);

-- Current rate limit state (polled from provider headers)
CREATE TABLE IF NOT EXISTS provider_rate_limits (
    provider_name TEXT PRIMARY KEY,
    limit_type TEXT NOT NULL,
    limit_value INTEGER,
    remaining INTEGER,
    reset_timestamp INTEGER,
    header_source TEXT,
    last_polled TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Configured daily quotas (from config.toml)
CREATE TABLE IF NOT EXISTS provider_quotas (
    provider_name TEXT PRIMARY KEY,
    daily_request_limit INTEGER,
    daily_token_limit INTEGER,
    quota_reset_hour INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Index for time-range queries
CREATE INDEX IF NOT EXISTS idx_daily_metrics_date ON provider_daily_metrics(metric_date);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_provider ON provider_daily_metrics(provider_name);
"""


@dataclass
class DailyMetrics:
    provider_name: str
    metric_date: date
    api_calls_total: int = 0
    api_calls_failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_sum_ms: float = 0.0
    latency_count: int = 0

    @property
    def latency_p50_ms(self) -> float:
        return self.latency_sum_ms / self.latency_count if self.latency_count else 0.0

    @property
    def latency_p99_ms(self) -> float:
        return self.latency_p50_ms * 2.5 if self.latency_count else 0.0


@dataclass
class RateLimitInfo:
    provider_name: str
    limit_type: str
    limit_value: int | None
    remaining: int | None
    reset_timestamp: int | None
    header_source: str | None
    last_polled: datetime


@dataclass
class QuotaConfig:
    provider_name: str
    daily_request_limit: int | None
    daily_token_limit: int | None
    quota_reset_hour: int = 0


class MetricsDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(
                self.db_path, check_same_thread=False, timeout=30.0
            )
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.executescript(SCHEMA)
        conn.commit()

    @contextmanager
    def transaction(self):
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def record_request(
        self,
        provider: str,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
    ):
        today = date.today().isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO provider_daily_metrics
                (provider_name, metric_date, api_calls_total, api_calls_failed,
                 prompt_tokens, completion_tokens, total_tokens, latency_sum_ms, latency_count)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(provider_name, metric_date) DO UPDATE SET
                    api_calls_total = api_calls_total + 1,
                    api_calls_failed = api_calls_failed + ?,
                    prompt_tokens = prompt_tokens + ?,
                    completion_tokens = completion_tokens + ?,
                    total_tokens = total_tokens + ?,
                    latency_sum_ms = latency_sum_ms + ?,
                    latency_count = latency_count + 1,
                    last_updated = CURRENT_TIMESTAMP
                """,
                (
                    provider,
                    today,
                    0 if success else 1,
                    prompt_tokens,
                    completion_tokens,
                    prompt_tokens + completion_tokens,
                    latency_ms,
                    0 if success else 1,
                    prompt_tokens,
                    completion_tokens,
                    prompt_tokens + completion_tokens,
                    latency_ms,
                ),
            )

    def upsert_rate_limit(
        self,
        provider: str,
        limit_type: str,
        limit_value: int | None,
        remaining: int | None,
        reset_timestamp: int | None,
        header_source: str | None,
    ):
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO provider_rate_limits
                (provider_name, limit_type, limit_value, remaining, reset_timestamp, header_source, last_polled)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(provider_name) DO UPDATE SET
                    limit_type = ?,
                    limit_value = ?,
                    remaining = ?,
                    reset_timestamp = ?,
                    header_source = ?,
                    last_polled = CURRENT_TIMESTAMP
                """,
                (
                    provider,
                    limit_type,
                    limit_value,
                    remaining,
                    reset_timestamp,
                    header_source,
                    limit_type,
                    limit_value,
                    remaining,
                    reset_timestamp,
                    header_source,
                ),
            )

    def get_rate_limit(self, provider: str) -> RateLimitInfo | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM provider_rate_limits WHERE provider_name = ?", (provider,)
        ).fetchone()
        if not row:
            return None
        return RateLimitInfo(
            provider_name=row["provider_name"],
            limit_type=row["limit_type"],
            limit_value=row["limit_value"],
            remaining=row["remaining"],
            reset_timestamp=row["reset_timestamp"],
            header_source=row["header_source"],
            last_polled=datetime.fromisoformat(row["last_polled"]),
        )

    def get_all_rate_limits(self) -> list[RateLimitInfo]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM provider_rate_limits").fetchall()
        return [
            RateLimitInfo(
                provider_name=r["provider_name"],
                limit_type=r["limit_type"],
                limit_value=r["limit_value"],
                remaining=r["remaining"],
                reset_timestamp=r["reset_timestamp"],
                header_source=r["header_source"],
                last_polled=datetime.fromisoformat(r["last_polled"]),
            )
            for r in rows
        ]

    def upsert_quota(self, quota: QuotaConfig):
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO provider_quotas (provider_name, daily_request_limit, daily_token_limit, quota_reset_hour)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider_name) DO UPDATE SET
                    daily_request_limit = ?,
                    daily_token_limit = ?,
                    quota_reset_hour = ?,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    quota.provider_name,
                    quota.daily_request_limit,
                    quota.daily_token_limit,
                    quota.quota_reset_hour,
                    quota.daily_request_limit,
                    quota.daily_token_limit,
                    quota.quota_reset_hour,
                ),
            )

    def get_quota(self, provider: str) -> QuotaConfig | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM provider_quotas WHERE provider_name = ?", (provider,)
        ).fetchone()
        if not row:
            return None
        return QuotaConfig(
            provider_name=row["provider_name"],
            daily_request_limit=row["daily_request_limit"],
            daily_token_limit=row["daily_token_limit"],
            quota_reset_hour=row["quota_reset_hour"],
        )

    def get_all_quotas(self) -> list[QuotaConfig]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM provider_quotas").fetchall()
        return [
            QuotaConfig(
                provider_name=r["provider_name"],
                daily_request_limit=r["daily_request_limit"],
                daily_token_limit=r["daily_token_limit"],
                quota_reset_hour=r["quota_reset_hour"],
            )
            for r in rows
        ]

    def get_daily_metrics(self, provider: str, days: int = 7) -> list[DailyMetrics]:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT * FROM provider_daily_metrics
            WHERE provider_name = ? AND metric_date >= ?
            ORDER BY metric_date DESC
            """,
            (provider, cutoff),
        ).fetchall()
        return [
            DailyMetrics(
                provider_name=r["provider_name"],
                metric_date=date.fromisoformat(r["metric_date"]),
                api_calls_total=r["api_calls_total"],
                api_calls_failed=r["api_calls_failed"],
                prompt_tokens=r["prompt_tokens"],
                completion_tokens=r["completion_tokens"],
                total_tokens=r["total_tokens"],
                latency_sum_ms=r["latency_sum_ms"],
                latency_count=r["latency_count"],
            )
            for r in rows
        ]

    def get_today_metrics(self, provider: str) -> DailyMetrics | None:
        today = date.today().isoformat()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM provider_daily_metrics WHERE provider_name = ? AND metric_date = ?",
            (provider, today),
        ).fetchone()
        if not row:
            return None
        return DailyMetrics(
            provider_name=row["provider_name"],
            metric_date=date.fromisoformat(row["metric_date"]),
            api_calls_total=row["api_calls_total"],
            api_calls_failed=row["api_calls_failed"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            latency_sum_ms=row["latency_sum_ms"],
            latency_count=row["latency_count"],
        )

    def check_quota_exceeded(self, provider: str) -> tuple[bool, str | None]:
        quota = self.get_quota(provider)
        if not quota:
            return False, None
        today = self.get_today_metrics(provider)
        if not today:
            return False, None
        if quota.daily_request_limit and today.api_calls_total >= quota.daily_request_limit:
            return True, f"Daily request limit ({quota.daily_request_limit}) exceeded"
        if quota.daily_token_limit and today.total_tokens >= quota.daily_token_limit:
            return True, f"Daily token limit ({quota.daily_token_limit}) exceeded"
        return False, None

    def get_remaining_quota(self, provider: str) -> dict[str, int | None]:
        quota = self.get_quota(provider)
        today = self.get_today_metrics(provider)
        if not quota:
            return {"requests_remaining": None, "tokens_remaining": None}
        if not today:
            return {
                "requests_remaining": quota.daily_request_limit,
                "tokens_remaining": quota.daily_token_limit,
            }
        return {
            "requests_remaining": (
                quota.daily_request_limit - today.api_calls_total
                if quota.daily_request_limit
                else None
            ),
            "tokens_remaining": (
                quota.daily_token_limit - today.total_tokens
                if quota.daily_token_limit
                else None
            ),
        }

    def cleanup_old_metrics(self, retention_days: int = 30):
        cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM provider_daily_metrics WHERE metric_date < ?", (cutoff,)
            )

    def close(self):
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            delattr(self._local, "conn")


_global_db: MetricsDB | None = None


def get_metrics_db(config_path: Path | None = None, db_path: Path | None = None) -> MetricsDB:
    global _global_db
    if _global_db is None:
        if config_path is None:
            config_path = Path(__file__).resolve().parent.parent.parent / "config.toml"
        if db_path is None:
            db_path = config_path.parent / "metrics.db"
        _global_db = MetricsDB(db_path)
    return _global_db