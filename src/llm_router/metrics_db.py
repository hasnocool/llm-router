# src/llm_router/metrics_db.py
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def _json_dumps(value: object) -> str:
    import json

    return json.dumps(value, default=str, ensure_ascii=False)


SCHEMA = """
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

CREATE TABLE IF NOT EXISTS provider_rate_limit_windows (
    provider_name TEXT NOT NULL,
    limit_type TEXT NOT NULL,
    limit_value INTEGER,
    remaining INTEGER,
    reset_timestamp INTEGER,
    header_source TEXT,
    last_polled TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider_name, limit_type)
);

CREATE TABLE IF NOT EXISTS provider_quotas (
    provider_name TEXT PRIMARY KEY,
    daily_request_limit INTEGER,
    daily_token_limit INTEGER,
    quota_reset_hour INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_request_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_name TEXT NOT NULL,
    occurred_at REAL NOT NULL,
    success INTEGER NOT NULL,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    status_code INTEGER,
    request_kind TEXT NOT NULL DEFAULT 'chat',
    response_kind TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS provider_quota_reservations (
    reservation_id TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    reserved_requests INTEGER NOT NULL DEFAULT 1,
    reserved_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS router_request_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL DEFAULT '',
    occurred_at REAL NOT NULL,
    provider_name TEXT,
    model TEXT NOT NULL DEFAULT '',
    task_kind TEXT NOT NULL DEFAULT 'general',
    stream INTEGER NOT NULL DEFAULT 0,
    explicit INTEGER NOT NULL DEFAULT 0,
    failover_index INTEGER NOT NULL DEFAULT 0,
    request_kind TEXT NOT NULL DEFAULT 'chat',
    response_kind TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    status_code INTEGER
);

CREATE INDEX IF NOT EXISTS idx_daily_metrics_date
    ON provider_daily_metrics(metric_date);
CREATE INDEX IF NOT EXISTS idx_daily_metrics_provider
    ON provider_daily_metrics(provider_name);
CREATE INDEX IF NOT EXISTS idx_request_events_provider_time
    ON provider_request_events(provider_name, occurred_at);
CREATE INDEX IF NOT EXISTS idx_quota_reservations_provider
    ON provider_quota_reservations(provider_name, expires_at);
CREATE INDEX IF NOT EXISTS idx_router_events_occurred
    ON router_request_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_router_events_provider
    ON router_request_events(provider_name);

CREATE TABLE IF NOT EXISTS app_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    source TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    request_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_app_events_time
    ON app_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_app_events_provider
    ON app_events(provider);

CREATE TABLE IF NOT EXISTS router_message_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL DEFAULT '',
    occurred_at REAL NOT NULL,
    provider_name TEXT,
    model TEXT NOT NULL DEFAULT '',
    stream INTEGER NOT NULL DEFAULT 0,
    explicit INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL,
    request_kind TEXT NOT NULL DEFAULT 'chat',
    response_kind TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    status_code INTEGER,
    request_json TEXT NOT NULL DEFAULT '',
    response_json TEXT NOT NULL DEFAULT '',
    error_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_message_logs_time
    ON router_message_logs(occurred_at);
CREATE INDEX IF NOT EXISTS idx_message_logs_request
    ON router_message_logs(request_id);
"""


class QuotaLimitExceeded(RuntimeError):
    """Raised when a local quota reservation would cross a configured limit."""


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
    latency_samples: tuple[float, ...] = field(default_factory=tuple)

    @staticmethod
    def _percentile(samples: tuple[float, ...], percentile: float) -> float:
        if not samples:
            return 0.0
        ordered = sorted(samples)
        if len(ordered) == 1:
            return float(ordered[0])
        pos = (len(ordered) - 1) * percentile
        lower = int(pos)
        upper = min(lower + 1, len(ordered) - 1)
        fraction = pos - lower
        return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)

    @property
    def latency_p50_ms(self) -> float:
        if self.latency_samples:
            return self._percentile(self.latency_samples, 0.50)
        return self.latency_sum_ms / self.latency_count if self.latency_count else 0.0

    @property
    def latency_p99_ms(self) -> float:
        if self.latency_samples:
            return self._percentile(self.latency_samples, 0.99)
        return self.latency_p50_ms if self.latency_count else 0.0


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
    """Blocking SQLite implementation. Call it only through AsyncMetricsStore at runtime."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version")
        self._migrate(conn)
        conn.commit()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after the original schema to existing databases."""
        events_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(provider_request_events)")
        }
        if "response_kind" not in events_cols:
            conn.execute(
                "ALTER TABLE provider_request_events "
                "ADD COLUMN response_kind TEXT NOT NULL DEFAULT ''"
            )
        router_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(router_request_events)")
        }
        if "request_id" not in router_cols:
            conn.execute(
                "ALTER TABLE router_request_events "
                "ADD COLUMN request_id TEXT NOT NULL DEFAULT ''"
            )
        if "task_kind" not in router_cols:
            conn.execute(
                "ALTER TABLE router_request_events "
                "ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'general'"
            )

    @contextmanager
    def transaction(self, *, immediate: bool = False):
        conn = self._get_conn()
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def quota_window_start(quota: QuotaConfig, now_ts: float | None = None) -> float:
        now = datetime.fromtimestamp(now_ts or time.time(), tz=timezone.utc)
        start = now.replace(
            hour=max(0, min(23, quota.quota_reset_hour)), minute=0, second=0, microsecond=0
        )
        if now < start:
            start -= timedelta(days=1)
        return start.timestamp()

    def _latency_samples(self, provider: str, metric_date: date) -> tuple[float, ...]:
        start = datetime.combine(metric_date, datetime.min.time(), tzinfo=timezone.utc).timestamp()
        end = start + 86400
        rows = self._get_conn().execute(
            """
            SELECT latency_ms FROM provider_request_events
            WHERE provider_name = ? AND occurred_at >= ? AND occurred_at < ? AND latency_ms >= 0
            ORDER BY latency_ms
            """,
            (provider, start, end),
        ).fetchall()
        return tuple(float(row["latency_ms"]) for row in rows)

    def _record_request_conn(
        self,
        conn: sqlite3.Connection,
        provider: str,
        success: bool,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        status_code: int | None,
        request_kind: str,
        occurred_at: float,
        response_kind: str = "",
    ) -> None:
        metric_date = datetime.fromtimestamp(occurred_at, tz=timezone.utc).date().isoformat()
        total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
        conn.execute(
            """
            INSERT INTO provider_request_events
            (provider_name, occurred_at, success, prompt_tokens, completion_tokens,
             total_tokens, latency_ms, status_code, request_kind, response_kind)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                occurred_at,
                1 if success else 0,
                max(0, prompt_tokens),
                max(0, completion_tokens),
                total_tokens,
                max(0.0, latency_ms),
                status_code,
                request_kind,
                response_kind,
            ),
        )
        conn.execute(
            """
            INSERT INTO provider_daily_metrics
            (provider_name, metric_date, api_calls_total, api_calls_failed,
             prompt_tokens, completion_tokens, total_tokens, latency_sum_ms, latency_count)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(provider_name, metric_date) DO UPDATE SET
                api_calls_total = api_calls_total + 1,
                api_calls_failed = api_calls_failed + excluded.api_calls_failed,
                prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                completion_tokens = completion_tokens + excluded.completion_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                latency_sum_ms = latency_sum_ms + excluded.latency_sum_ms,
                latency_count = latency_count + 1,
                last_updated = CURRENT_TIMESTAMP
            """,
            (
                provider,
                metric_date,
                0 if success else 1,
                max(0, prompt_tokens),
                max(0, completion_tokens),
                total_tokens,
                max(0.0, latency_ms),
            ),
        )

    def record_request(
        self,
        provider: str,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status_code: int | None = None,
        request_kind: str = "chat",
        response_kind: str = "",
    ) -> None:
        with self.transaction() as conn:
            self._record_request_conn(
                conn,
                provider,
                success,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                status_code,
                request_kind,
                time.time(),
                response_kind=response_kind,
            )

    def upsert_rate_limits(self, provider: str, limits: Iterable[object]) -> None:
        with self.transaction() as conn:
            for item in limits:
                conn.execute(
                    """
                    INSERT INTO provider_rate_limit_windows
                    (provider_name, limit_type, limit_value, remaining, reset_timestamp,
                     header_source, last_polled)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(provider_name, limit_type) DO UPDATE SET
                        limit_value = excluded.limit_value,
                        remaining = excluded.remaining,
                        reset_timestamp = excluded.reset_timestamp,
                        header_source = excluded.header_source,
                        last_polled = CURRENT_TIMESTAMP
                    """,
                    (
                        provider,
                        getattr(item, "limit_type"),
                        getattr(item, "limit_value"),
                        getattr(item, "remaining"),
                        getattr(item, "reset_timestamp"),
                        getattr(item, "header_source"),
                    ),
                )

    def get_rate_limits(self, provider: str) -> list[RateLimitInfo]:
        rows = self._get_conn().execute(
            "SELECT * FROM provider_rate_limit_windows WHERE provider_name = ?",
            (provider,),
        ).fetchall()
        return [self._rate_limit_from_row(row) for row in rows]

    def get_rate_limit(self, provider: str, limit_type: str | None = None) -> RateLimitInfo | None:
        conn = self._get_conn()
        if limit_type:
            row = conn.execute(
                """SELECT * FROM provider_rate_limit_windows
                WHERE provider_name = ? AND limit_type = ?""",
                (provider, limit_type),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM provider_rate_limit_windows
                WHERE provider_name = ?
                ORDER BY CASE WHEN remaining IS NULL THEN 1 ELSE 0 END, remaining ASC
                LIMIT 1
                """,
                (provider,),
            ).fetchone()
        return self._rate_limit_from_row(row) if row else None

    def get_all_rate_limits(self) -> list[RateLimitInfo]:
        rows = self._get_conn().execute(
            "SELECT * FROM provider_rate_limit_windows ORDER BY provider_name, limit_type"
        ).fetchall()
        return [self._rate_limit_from_row(row) for row in rows]

    @staticmethod
    def _rate_limit_from_row(row: sqlite3.Row) -> RateLimitInfo:
        return RateLimitInfo(
            provider_name=row["provider_name"],
            limit_type=row["limit_type"],
            limit_value=row["limit_value"],
            remaining=row["remaining"],
            reset_timestamp=row["reset_timestamp"],
            header_source=row["header_source"],
            last_polled=datetime.fromisoformat(row["last_polled"]),
        )

    def upsert_quota(self, quota: QuotaConfig) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO provider_quotas
                (provider_name, daily_request_limit, daily_token_limit, quota_reset_hour)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider_name) DO UPDATE SET
                    daily_request_limit = excluded.daily_request_limit,
                    daily_token_limit = excluded.daily_token_limit,
                    quota_reset_hour = excluded.quota_reset_hour,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    quota.provider_name,
                    quota.daily_request_limit,
                    quota.daily_token_limit,
                    quota.quota_reset_hour,
                ),
            )

    def get_quota(self, provider: str) -> QuotaConfig | None:
        row = self._get_conn().execute(
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
        rows = self._get_conn().execute("SELECT * FROM provider_quotas").fetchall()
        return [
            QuotaConfig(
                provider_name=row["provider_name"],
                daily_request_limit=row["daily_request_limit"],
                daily_token_limit=row["daily_token_limit"],
                quota_reset_hour=row["quota_reset_hour"],
            )
            for row in rows
        ]

    def _window_usage_conn(
        self, conn: sqlite3.Connection, provider: str, quota: QuotaConfig, now_ts: float
    ) -> tuple[int, int, int, int]:
        window_start = self.quota_window_start(quota, now_ts)
        event = conn.execute(
            """
            SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens), 0) AS tokens
            FROM provider_request_events
            WHERE provider_name = ? AND occurred_at >= ? AND occurred_at <= ?
            """,
            (provider, window_start, now_ts),
        ).fetchone()
        reservation = conn.execute(
            """
            SELECT COALESCE(SUM(reserved_requests), 0) AS calls,
                   COALESCE(SUM(reserved_tokens), 0) AS tokens
            FROM provider_quota_reservations
            WHERE provider_name = ? AND expires_at > ?
            """,
            (provider, now_ts),
        ).fetchone()
        return (
            int(event["calls"] or 0),
            int(event["tokens"] or 0),
            int(reservation["calls"] or 0),
            int(reservation["tokens"] or 0),
        )

    def reserve_quota(
        self,
        provider: str,
        estimated_tokens: int = 0,
        ttl_seconds: int = 600,
    ) -> str | None:
        quota = self.get_quota(provider)
        if not quota or (quota.daily_request_limit is None and quota.daily_token_limit is None):
            return None
        now_ts = time.time()
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM provider_quota_reservations WHERE expires_at <= ?", (now_ts,))
            calls, tokens, reserved_calls, reserved_tokens = self._window_usage_conn(
                conn, provider, quota, now_ts
            )
            next_calls = calls + reserved_calls + 1
            next_tokens = tokens + reserved_tokens + max(0, estimated_tokens)
            if quota.daily_request_limit is not None and next_calls > quota.daily_request_limit:
                raise QuotaLimitExceeded(
                    f"Daily request limit ({quota.daily_request_limit}) would be exceeded"
                )
            if quota.daily_token_limit is not None and next_tokens > quota.daily_token_limit:
                raise QuotaLimitExceeded(
                    f"Daily token limit ({quota.daily_token_limit}) would be exceeded"
                )
            reservation_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO provider_quota_reservations
                (reservation_id, provider_name, created_at, expires_at, reserved_requests, reserved_tokens)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (
                    reservation_id,
                    provider,
                    now_ts,
                    now_ts + max(30, ttl_seconds),
                    max(0, estimated_tokens),
                ),
            )
            return reservation_id

    def reconcile_reservation(
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
        with self.transaction(immediate=True) as conn:
            if reservation_id:
                conn.execute(
                    "DELETE FROM provider_quota_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                )
            self._record_request_conn(
                conn,
                provider,
                success,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                status_code,
                request_kind,
                time.time(),
                response_kind=response_kind,
            )

    def record_router_event(
        self,
        provider: str | None,
        model: str,
        stream: bool,
        explicit: bool,
        failover_index: int,
        success: bool,
        task_kind: str = "general",
        request_kind: str = "chat",
        response_kind: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status_code: int | None = None,
        occurred_at: float | None = None,
        request_id: str = "",
    ) -> None:
        with self.transaction() as conn:
            total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
            conn.execute(
                """
                INSERT INTO router_request_events
                (occurred_at, provider_name, model, task_kind, stream, explicit, failover_index,
                 request_kind, response_kind, success, prompt_tokens, completion_tokens,
                 total_tokens, latency_ms, status_code, request_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time() if occurred_at is None else occurred_at,
                    provider,
                    model,
                    task_kind,
                    1 if stream else 0,
                    1 if explicit else 0,
                    failover_index,
                    request_kind,
                    response_kind,
                    1 if success else 0,
                    max(0, prompt_tokens),
                    max(0, completion_tokens),
                    total_tokens,
                    max(0.0, latency_ms),
                    status_code,
                    request_id,
                ),
            )

    def get_recent_router_events(self, limit: int = 100) -> list[dict[str, object]]:
        rows = self._get_conn().execute(
            """
            SELECT * FROM router_request_events
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_provider_attempt_metrics(self, days: int = 7) -> list[dict[str, object]]:
        cutoff = time.time() - days * 86400
        rows = self._get_conn().execute(
            """
            SELECT
                provider_name,
                COUNT(*) AS attempts,
                SUM(success) AS successes,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                SUM(prompt_tokens) AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                SUM(total_tokens) AS total_tokens,
                AVG(latency_ms) AS avg_latency_ms,
                MIN(latency_ms) AS min_latency_ms,
                MAX(latency_ms) AS max_latency_ms
            FROM provider_request_events
            WHERE occurred_at >= ?
            GROUP BY provider_name
            ORDER BY attempts DESC, provider_name
            """,
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_task_breakdown(self, days: int = 30) -> dict[str, int]:
        cutoff = time.time() - days * 86400
        rows = self._get_conn().execute(
            """
            SELECT task_kind, COUNT(*) AS count
            FROM router_request_events
            WHERE occurred_at >= ?
            GROUP BY task_kind
            """,
            (cutoff,),
        ).fetchall()
        return {row["task_kind"]: int(row["count"]) for row in rows}

    def get_router_attempt_metrics(self, days: int = 7) -> list[dict[str, object]]:
        cutoff = time.time() - days * 86400
        rows = self._get_conn().execute(
            """
            SELECT
                provider_name,
                COUNT(*) AS requests,
                SUM(success) AS successes,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                SUM(CASE WHEN failover_index > 0 THEN 1 ELSE 0 END) AS failovers,
                SUM(CASE WHEN stream = 1 THEN 1 ELSE 0 END) AS streams,
                SUM(CASE WHEN explicit = 1 THEN 1 ELSE 0 END) AS explicit_requests,
                SUM(prompt_tokens) AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                SUM(total_tokens) AS total_tokens,
                AVG(latency_ms) AS avg_latency_ms
            FROM router_request_events
            WHERE occurred_at >= ? AND provider_name IS NOT NULL
            GROUP BY provider_name
            ORDER BY requests DESC, provider_name
            """,
            (cutoff,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_request_timeline(self, days: int = 30) -> list[dict[str, object]]:
        cutoff = time.time() - days * 86400
        provider_rows = self._get_conn().execute(
            """
            SELECT
                date(occurred_at, 'unixepoch') AS day,
                COUNT(*) AS attempts,
                SUM(success) AS successes,
                SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures,
                SUM(prompt_tokens) AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                SUM(total_tokens) AS total_tokens,
                AVG(latency_ms) AS avg_latency_ms
            FROM provider_request_events
            WHERE occurred_at >= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (cutoff,),
        ).fetchall()
        router_rows = self._get_conn().execute(
            """
            SELECT
                date(occurred_at, 'unixepoch') AS day,
                SUM(CASE WHEN failover_index > 0 THEN 1 ELSE 0 END) AS failovers
            FROM router_request_events
            WHERE occurred_at >= ?
            GROUP BY day
            ORDER BY day ASC
            """,
            (cutoff,),
        ).fetchall()
        timeline: dict[str, dict[str, object]] = {}
        for row in provider_rows:
            day = str(row["day"])
            timeline[day] = {
                "day": day,
                "attempts": int(row["attempts"] or 0),
                "successes": int(row["successes"] or 0),
                "failures": int(row["failures"] or 0),
                "failovers": 0,
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "avg_latency_ms": float(row["avg_latency_ms"] or 0.0),
            }
        for row in router_rows:
            day = str(row["day"])
            bucket = timeline.setdefault(
                day,
                {
                    "day": day,
                    "attempts": 0,
                    "successes": 0,
                    "failures": 0,
                    "failovers": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "avg_latency_ms": 0.0,
                },
            )
            bucket["failovers"] = int(row["failovers"] or 0)
        return [timeline[key] for key in sorted(timeline)]

    def get_app_event_breakdown(self, days: int = 7) -> dict[str, dict[str, int]]:
        cutoff = time.time() - days * 86400
        rows = self._get_conn().execute(
            """
            SELECT level, source, COUNT(*) AS count
            FROM app_events
            WHERE occurred_at >= ?
            GROUP BY level, source
            """,
            (cutoff,),
        ).fetchall()
        levels: dict[str, int] = {}
        sources: dict[str, int] = {}
        for row in rows:
            count = int(row["count"])
            levels[row["level"]] = levels.get(row["level"], 0) + count
            sources[row["source"]] = sources.get(row["source"], 0) + count
        return {"levels": levels, "sources": sources}

    def record_app_event(
        self,
        *,
        level: str,
        source: str,
        message: str,
        provider: str | None = None,
        model: str | None = None,
        request_id: str = "",
        details: dict[str, object] | None = None,
        occurred_at: float | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO app_events
                (occurred_at, level, source, message, provider, model, request_id, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time() if occurred_at is None else occurred_at,
                    level,
                    source,
                    message,
                    provider,
                    model,
                    request_id,
                    _json_dumps(details or {}),
                ),
            )

    def get_app_events(
        self,
        limit: int = 200,
        *,
        level: str | None = None,
        provider: str | None = None,
        request_id: str | None = None,
        since: float | None = None,
        levels: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if level:
            clauses.append("level = ?")
            params.append(level)
        if levels:
            clauses.append(f"level IN ({','.join('?' for _ in levels)})")
            params.extend(levels)
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if request_id:
            clauses.append("request_id = ?")
            params.append(request_id)
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._get_conn().execute(
            f"""
            SELECT * FROM app_events
            {where}
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (*params, max(1, limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_message_log(
        self,
        *,
        request_id: str = "",
        provider: str | None,
        model: str,
        stream: bool,
        explicit: bool,
        success: bool,
        request_kind: str = "chat",
        response_kind: str = "",
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status_code: int | None = None,
        request_json: str = "",
        response_json: str = "",
        error_json: str = "",
        occurred_at: float | None = None,
    ) -> None:
        with self.transaction() as conn:
            total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
            conn.execute(
                """
                INSERT INTO router_message_logs
                (request_id, occurred_at, provider_name, model, stream, explicit, success,
                 request_kind, response_kind, prompt_tokens, completion_tokens, total_tokens,
                 latency_ms, status_code, request_json, response_json, error_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    time.time() if occurred_at is None else occurred_at,
                    provider,
                    model,
                    1 if stream else 0,
                    1 if explicit else 0,
                    1 if success else 0,
                    request_kind,
                    response_kind,
                    max(0, prompt_tokens),
                    max(0, completion_tokens),
                    total_tokens,
                    max(0.0, latency_ms),
                    status_code,
                    request_json,
                    response_json,
                    error_json,
                ),
            )

    def get_message_logs(
        self,
        limit: int = 200,
        *,
        provider: str | None = None,
        model: str | None = None,
        request_id: str | None = None,
        since: float | None = None,
        success: bool | None = None,
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if provider:
            clauses.append("provider_name = ?")
            params.append(provider)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if request_id:
            clauses.append("request_id = ?")
            params.append(request_id)
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(since)
        if success is not None:
            clauses.append("success = ?")
            params.append(1 if success else 0)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._get_conn().execute(
            f"""
            SELECT * FROM router_message_logs
            {where}
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (*params, max(1, limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_kind_breakdown(self, days: int = 7) -> dict[str, dict[str, int]]:
        cutoff = time.time() - days * 86400
        conn = self._get_conn()
        request_rows = conn.execute(
            """
            SELECT request_kind, COUNT(*) AS count
            FROM provider_request_events
            WHERE occurred_at >= ?
            GROUP BY request_kind
            """,
            (cutoff,),
        ).fetchall()
        response_rows = conn.execute(
            """
            SELECT response_kind, COUNT(*) AS count
            FROM provider_request_events
            WHERE occurred_at >= ?
            GROUP BY response_kind
            """,
            (cutoff,),
        ).fetchall()
        return {
            "request": {row["request_kind"]: int(row["count"]) for row in request_rows},
            "response": {row["response_kind"]: int(row["count"]) for row in response_rows},
        }

    def cancel_reservation(self, reservation_id: str | None) -> None:
        if not reservation_id:
            return
        with self.transaction(immediate=True) as conn:
            conn.execute(
                "DELETE FROM provider_quota_reservations WHERE reservation_id = ?",
                (reservation_id,),
            )

    def check_quota_exceeded(self, provider: str) -> tuple[bool, str | None]:
        quota = self.get_quota(provider)
        if not quota:
            return False, None
        calls, tokens, reserved_calls, reserved_tokens = self._window_usage_conn(
            self._get_conn(), provider, quota, time.time()
        )
        if quota.daily_request_limit is not None and calls + reserved_calls >= quota.daily_request_limit:
            return True, f"Daily request limit ({quota.daily_request_limit}) exceeded"
        if quota.daily_token_limit is not None and tokens + reserved_tokens >= quota.daily_token_limit:
            return True, f"Daily token limit ({quota.daily_token_limit}) exceeded"
        return False, None

    def get_remaining_quota(self, provider: str) -> dict[str, int | None]:
        quota = self.get_quota(provider)
        if not quota:
            return {"requests_remaining": None, "tokens_remaining": None}
        calls, tokens, reserved_calls, reserved_tokens = self._window_usage_conn(
            self._get_conn(), provider, quota, time.time()
        )
        return {
            "requests_remaining": (
                max(0, quota.daily_request_limit - calls - reserved_calls)
                if quota.daily_request_limit is not None
                else None
            ),
            "tokens_remaining": (
                max(0, quota.daily_token_limit - tokens - reserved_tokens)
                if quota.daily_token_limit is not None
                else None
            ),
        }

    def get_daily_metrics(self, provider: str, days: int = 7) -> list[DailyMetrics]:
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=max(0, days - 1))).isoformat()
        rows = self._get_conn().execute(
            """
            SELECT * FROM provider_daily_metrics
            WHERE provider_name = ? AND metric_date >= ?
            ORDER BY metric_date DESC
            """,
            (provider, cutoff),
        ).fetchall()
        result: list[DailyMetrics] = []
        for row in rows:
            metric_date = date.fromisoformat(row["metric_date"])
            result.append(
                DailyMetrics(
                    provider_name=row["provider_name"],
                    metric_date=metric_date,
                    api_calls_total=row["api_calls_total"],
                    api_calls_failed=row["api_calls_failed"],
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    total_tokens=row["total_tokens"],
                    latency_sum_ms=row["latency_sum_ms"],
                    latency_count=row["latency_count"],
                    latency_samples=self._latency_samples(provider, metric_date),
                )
            )
        return result

    def get_today_metrics(self, provider: str) -> DailyMetrics | None:
        today = datetime.now(timezone.utc).date()
        row = self._get_conn().execute(
            "SELECT * FROM provider_daily_metrics WHERE provider_name = ? AND metric_date = ?",
            (provider, today.isoformat()),
        ).fetchone()
        if not row:
            return None
        return DailyMetrics(
            provider_name=row["provider_name"],
            metric_date=today,
            api_calls_total=row["api_calls_total"],
            api_calls_failed=row["api_calls_failed"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            latency_sum_ms=row["latency_sum_ms"],
            latency_count=row["latency_count"],
            latency_samples=self._latency_samples(provider, today),
        )

    def cleanup_old_metrics(self, retention_days: int = 30) -> None:
        cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=retention_days)).isoformat()
        cutoff_ts = time.time() - retention_days * 86400
        now_ts = time.time()
        with self.transaction(immediate=True) as conn:
            conn.execute("DELETE FROM provider_daily_metrics WHERE metric_date < ?", (cutoff_date,))
            conn.execute("DELETE FROM provider_request_events WHERE occurred_at < ?", (cutoff_ts,))
            conn.execute("DELETE FROM router_request_events WHERE occurred_at < ?", (cutoff_ts,))
            conn.execute("DELETE FROM router_message_logs WHERE occurred_at < ?", (cutoff_ts,))
            conn.execute("DELETE FROM app_events WHERE occurred_at < ?", (cutoff_ts,))
            conn.execute("DELETE FROM provider_quota_reservations WHERE expires_at <= ?", (now_ts,))

    def close(self) -> None:
        if hasattr(self._local, "conn"):
            self._local.conn.close()
            delattr(self._local, "conn")
