# tests/test_operational_correctness.py
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from llm_router.async_metrics import AsyncMetricsStore
from llm_router.config import MetricsConfig, ProviderConfig, QuotaConfig as SettingsQuota, Settings
from llm_router.metrics_db import MetricsDB, QuotaConfig, QuotaLimitExceeded
from llm_router.providers.google_ai import GoogleAIProvider
from llm_router.rate_limits import RateLimitData, RateLimitParser, parse_reset_timestamp
from llm_router.router import ModelRouter


def test_rate_limit_windows_do_not_overwrite_each_other(tmp_path):
    db = MetricsDB(tmp_path / "metrics.db")
    db.upsert_rate_limits(
        "groq",
        [
            RateLimitData("requests", 1000, 999, 2_000_000_000, "requests"),
            RateLimitData("tokens", 100000, 90000, 2_000_000_100, "tokens"),
        ],
    )
    limits = {item.limit_type: item for item in db.get_rate_limits("groq")}
    assert limits["requests"].remaining == 999
    assert limits["tokens"].remaining == 90000


def test_reset_hour_window_math_is_utc():
    quota = QuotaConfig("groq", 100, 1000, quota_reset_hour=6)
    now = datetime(2026, 8, 10, 5, 30, tzinfo=timezone.utc).timestamp()
    start = datetime.fromtimestamp(MetricsDB.quota_window_start(quota, now), tz=timezone.utc)
    assert start == datetime(2026, 8, 9, 6, 0, tzinfo=timezone.utc)


def test_atomic_reservation_blocks_second_request(tmp_path):
    db = MetricsDB(tmp_path / "metrics.db")
    db.upsert_quota(QuotaConfig("groq", 1, 1000, 0))
    first = db.reserve_quota("groq", estimated_tokens=10)
    assert first
    with pytest.raises(QuotaLimitExceeded):
        db.reserve_quota("groq", estimated_tokens=10)
    db.cancel_reservation(first)
    assert db.reserve_quota("groq", estimated_tokens=10)


def test_latency_percentiles_use_samples(tmp_path):
    db = MetricsDB(tmp_path / "metrics.db")
    for latency in (100.0, 200.0, 300.0):
        db.record_request("groq", True, latency_ms=latency)
    today = db.get_today_metrics("groq")
    assert today is not None
    assert today.latency_p50_ms == pytest.approx(200.0)
    assert today.latency_p99_ms > 290.0


def test_duration_rate_limit_reset_and_malformed_headers_are_safe():
    now = 1_000.0
    assert parse_reset_timestamp("6m0s", now=now) == 1_360
    assert parse_reset_timestamp("not-a-reset", now=now) is None
    parsed = RateLimitParser.parse_openai_compatible(
        {
            "x-ratelimit-remaining-requests": "9",
            "x-ratelimit-limit-requests": "10",
            "x-ratelimit-reset-requests": "1m",
            "x-ratelimit-remaining-tokens": "oops",
        }
    )
    assert [item.limit_type for item in parsed] == ["requests"]


def test_gemini_system_and_assistant_roles_are_preserved():
    provider = GoogleAIProvider(
        "google_ai",
        ProviderConfig("Google", "https://example.invalid", "gemini"),
        httpx.AsyncClient(),
    )
    payload = provider._openai_to_gemini_payload(
        {
            "messages": [
                {"role": "system", "content": "be concise"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
        }
    )
    assert payload["systemInstruction"]["parts"][0]["text"] == "be concise"
    assert [item["role"] for item in payload["contents"]] == ["user", "model"]


def test_gemini_stream_chunk_is_openai_compatible():
    provider = GoogleAIProvider(
        "google_ai",
        ProviderConfig("Google", "https://example.invalid", "gemini"),
        httpx.AsyncClient(),
    )
    line = provider._gemini_stream_chunk(
        {
            "candidates": [{
                "content": {"parts": [{"text": "hello"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1},
        },
        "gemini",
    )
    assert line is not None and line.startswith("data: ")
    assert '"object":"chat.completion.chunk"' in line
    assert '"content":"hello"' in line


class CountingModelsProvider:
    def __init__(self):
        self.calls = 0

    async def models(self):
        self.calls += 1
        return [{"id": "m1"}]


def test_model_discovery_is_cached_and_background_start_does_not_probe(tmp_path):
    async def run():
        settings = Settings(
            providers={"huggingface": ProviderConfig("HF", "https://hf.example", "m1")},
            quotas={"huggingface": SettingsQuota(daily_request_limit=10)},
            metrics=MetricsConfig(
                db_path=str(tmp_path / "metrics.db"),
                report_interval_seconds=0,
                model_cache_ttl_seconds=3600,
            ),
        )
        async with httpx.AsyncClient() as client:
            router = ModelRouter(settings, client)
            counter = CountingModelsProvider()
            router.providers["huggingface"] = counter
            await router.start_background_tasks()
            await asyncio.sleep(0)
            assert counter.calls == 0
            await router.list_models()
            await router.list_models()
            assert counter.calls == 1
            await router.stop_background_tasks()

    asyncio.run(run())


def test_async_store_serializes_concurrent_reservations(tmp_path):
    async def run():
        store = AsyncMetricsStore(tmp_path / "metrics.db")
        await store.start()
        await store.upsert_quota(QuotaConfig("groq", 1, 1000, 0))

        async def reserve():
            try:
                return await store.reserve_quota("groq", 1)
            except QuotaLimitExceeded:
                return None

        first, second = await asyncio.gather(reserve(), reserve())
        assert sum(value is not None for value in (first, second)) == 1
        await store.close()

    asyncio.run(run())
