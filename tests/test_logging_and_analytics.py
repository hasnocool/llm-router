# tests/test_logging_and_analytics.py
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from llm_router import dashboard, main
from llm_router.config import AnalyticsConfig, MetricsConfig, ProviderConfig, ProviderPricing, Settings
from llm_router.log_events import APP_EVENT_BUFFER, DBEventLogHandler, _AppEventBuffer, attach_event_context, reset_event_context
from llm_router.router import ModelRouter


def test_event_handler_captures_context():
    APP_EVENT_BUFFER.drain()
    handler = DBEventLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord(
        name="root",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.source = "router"

    token = attach_event_context(provider="groq", model="m1", request_id="abc123")
    try:
        handler.emit(record)
    finally:
        reset_event_context(token)

    event = APP_EVENT_BUFFER.drain()[-1]
    assert event["provider"] == "groq"
    assert event["model"] == "m1"
    assert event["request_id"] == "abc123"
    assert event["source"] == "router"
    assert event["message"] == "hello world"


def test_event_buffer_overflow_records_warning():
    buf = _AppEventBuffer(maxsize=1)
    buf.put({"level": "info", "source": "app", "message": "one"})
    buf.put({"level": "info", "source": "app", "message": "two"})
    events = buf.drain()
    assert events[0]["message"] == "one"
    assert events[1]["message"] == "event log buffer overflowed; dropped 1 events"


def test_logs_api_routes_to_router(monkeypatch):
    captured: dict[str, object] = {}

    class StubRouter:
        async def get_logs_data(self, **kwargs):
            captured.update(kwargs)
            return {"events": [{"source": "router"}], "messages": [{"provider_name": "groq"}]}

    monkeypatch.setattr(main, "_router", StubRouter())
    response = asyncio.run(dashboard.logs_api(days=7, limit=50, level="error", provider="groq", request_id="rid", view="messages"))
    payload = json.loads(response.body)
    assert captured["messages_only"] is True
    assert captured["events_only"] is False
    assert payload["messages"][0]["provider_name"] == "groq"


def test_analytics_api_routes_to_router(monkeypatch):
    captured: dict[str, object] = {}

    class StubRouter:
        async def get_analytics_data(self, days: int = 30):
            captured["days"] = days
            return {"summary": {"attempts": 1}, "providers": {}, "alerts": []}

    monkeypatch.setattr(main, "_router", StubRouter())
    response = asyncio.run(dashboard.analytics_api(days=14))
    payload = json.loads(response.body)
    assert captured["days"] == 14
    assert payload["summary"]["attempts"] == 1


def test_analytics_summary_includes_cost_and_alerts(tmp_path):
    async def run():
        settings = Settings(
            providers={"groq": ProviderConfig("Groq", "https://example.invalid", "m1")},
            analytics=AnalyticsConfig(
                currency="USD",
                default_input_cost_per_1m_tokens=1.0,
                default_output_cost_per_1m_tokens=1.0,
                error_rate_warning_threshold=0.05,
                error_rate_critical_threshold=0.10,
                failover_rate_warning_threshold=0.10,
                pricing={"groq": ProviderPricing(1.0, 1.0)},
            ),
            metrics=MetricsConfig(db_path=str(tmp_path / "metrics.db"), report_interval_seconds=0),
        )
        async with httpx.AsyncClient() as client:
            router = ModelRouter(settings, client)
            for idx in range(5):
                success = idx < 4
                await router._metrics_store.record_request(
                    provider="groq",
                    success=success,
                    prompt_tokens=1_000_000,
                    completion_tokens=0,
                    latency_ms=100.0 + idx,
                    request_kind="chat",
                    response_kind="chat",
                )
                await router._metrics_store.record_router_event(
                    provider="groq",
                    model="m1",
                    stream=False,
                    explicit=True,
                    failover_index=1 if not success else 0,
                    success=success,
                    request_kind="chat",
                    response_kind="chat",
                    prompt_tokens=1_000_000,
                    completion_tokens=0,
                    latency_ms=100.0 + idx,
                    request_id=f"rid-{idx}",
                )
            data = await router.get_analytics_data(days=7)
            await router._metrics_store.close()

        assert data["summary"]["attempts"] == 5
        assert data["summary"]["failures"] == 1
        assert data["summary"]["estimated_cost_usd"] == pytest.approx(5.0)
        assert data["providers"]["groq"]["success_rate"] == pytest.approx(0.8)
        assert data["providers"]["groq"]["failover_rate"] == pytest.approx(0.2)
        assert len(data["timeline"]) == 1
        assert data["timeline"][0]["attempts"] == 5
        assert any(alert["severity"] in {"warning", "critical"} for alert in data["alerts"])

    asyncio.run(run())


def test_logs_data_filters_by_request_id_and_provider(tmp_path):
    async def run():
        settings = Settings(
            metrics=MetricsConfig(db_path=str(tmp_path / "metrics.db"), report_interval_seconds=0),
        )
        async with httpx.AsyncClient() as client:
            router = ModelRouter(settings, client)
            await router._metrics_store.record_app_event(
                level="info",
                source="router",
                message="provider groq served",
                provider="groq",
                model="m1",
                request_id="rid-1",
            )
            await router._metrics_store.record_app_event(
                level="info",
                source="router",
                message="provider hf served",
                provider="huggingface",
                model="m2",
                request_id="rid-2",
            )
            await router._metrics_store.record_message_log(
                request_id="rid-1",
                provider="groq",
                model="m1",
                stream=False,
                explicit=True,
                success=True,
                request_kind="chat",
                response_kind="chat",
                prompt_tokens=10,
                completion_tokens=20,
                latency_ms=50.0,
                request_json="{}",
                response_json="{}",
            )
            data = await router.get_logs_data(days=7, limit=20, provider="groq", request_id="rid-1")
            await router._metrics_store.close()

        assert len(data["events"]) == 1
        assert data["events"][0]["provider"] == "groq"
        assert len(data["messages"]) == 1
        assert data["messages"][0]["request_id"] == "rid-1"

    asyncio.run(run())
