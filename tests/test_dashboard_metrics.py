# tests/test_dashboard_metrics.py
from __future__ import annotations

import asyncio

from llm_router.metrics_db import MetricsDB
from llm_router.providers.base import classify_request_kind, classify_response_kind
from llm_router.router import ModelRouter
from llm_router.config import MetricsConfig, ProviderConfig, Settings


def test_classify_request_kind():
    assert classify_request_kind({}) == "chat"
    assert classify_request_kind({"messages": []}) == "chat"
    assert classify_request_kind({"tools": [{"type": "function"}]}) == "tool_call"
    assert classify_request_kind({"tool_choice": "auto"}) == "tool_call"
    assert classify_request_kind({"tools": [], "tool_choice": "none"}) == "chat"


def test_classify_response_kind():
    assert classify_response_kind({}) == "chat"
    assert classify_response_kind({"choices": []}) == "chat"
    assert classify_response_kind(
        {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
    ) == "chat"
    assert classify_response_kind(
        {"choices": [{"message": {"content": "hi"}, "finish_reason": "tool_calls"}]}
    ) == "tool_call"
    assert classify_response_kind(
        {"choices": [{"message": {"tool_calls": [{"id": "x"}]}}]}
    ) == "tool_call"


def test_router_event_recording_and_breakdown(tmp_path):
    db = MetricsDB(tmp_path / "metrics.db")
    db.record_router_event(
        provider="groq",
        model="llama-3.3-70b",
        stream=False,
        explicit=True,
        failover_index=0,
        success=True,
        request_kind="tool_call",
        response_kind="tool_call",
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=123.4,
        occurred_at=1000.0,
    )
    db.record_router_event(
        provider=None,
        model="qwen3-8b",
        stream=True,
        explicit=False,
        failover_index=3,
        success=False,
        request_kind="chat",
        response_kind="",
        occurred_at=900.0,
    )
    events = db.get_recent_router_events(limit=10)
    assert len(events) == 2
    assert events[0]["provider_name"] == "groq"
    assert events[0]["request_kind"] == "tool_call"
    assert events[0]["total_tokens"] == 150
    assert events[0]["explicit"] == 1
    assert events[1]["provider_name"] is None
    assert events[1]["failover_index"] == 3
    assert events[1]["success"] == 0


def test_response_kind_column_migrates_existing_db(tmp_path):
    import sqlite3

    path = tmp_path / "metrics.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE provider_request_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_name TEXT NOT NULL,
            occurred_at REAL NOT NULL,
            success INTEGER NOT NULL,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            latency_ms REAL DEFAULT 0,
            status_code INTEGER,
            request_kind TEXT NOT NULL DEFAULT 'inference'
        );
        """
    )
    conn.commit()
    conn.close()

    db2 = MetricsDB(path)
    db2.record_request(
        "groq",
        True,
        prompt_tokens=5,
        completion_tokens=5,
        request_kind="tool_call",
        response_kind="chat",
    )
    breakdown = db2.get_kind_breakdown(days=7)
    assert breakdown["request"]["tool_call"] == 1
    assert breakdown["response"]["chat"] == 1


def test_dashboard_aggregation_shapes(tmp_path):
    async def run():
        settings = Settings(
            providers={"groq": ProviderConfig("Groq", "https://groq.example", "m1")},
            metrics=MetricsConfig(
                db_path=str(tmp_path / "metrics.db"),
                report_interval_seconds=0,
            ),
        )
        from httpx import AsyncClient

        async with AsyncClient() as client:
            router = ModelRouter(settings, client)
            await router.start_background_tasks()
            db = await router._metrics_store.run_blocking(lambda d: d)
            db.record_request("groq", True, prompt_tokens=10, completion_tokens=10)
            db.record_request("groq", False, prompt_tokens=0, completion_tokens=0)
            db.record_router_event(
                provider="groq",
                model="m1",
                stream=False,
                explicit=True,
                failover_index=0,
                success=True,
                request_kind="chat",
                response_kind="chat",
                prompt_tokens=10,
                completion_tokens=10,
                latency_ms=50.0,
            )
            await router.refresh_status_from_metrics()
            data = await router.get_dashboard_data(days=7, events=10)
            assert data["strategy"] == "cloud-first"
            assert data["summary"]["calls_today"] == 2
            assert data["summary"]["failed_today"] == 1
            assert data["summary"]["tokens_today"] == 20
            assert data["summary"]["kind_breakdown"]["request"]["chat"] == 2
            assert data["providers"]["groq"]["daily_calls_used"] == 2
            assert len(data["recent_events"]) == 1
            assert len(data["models"]) >= 1
            await router.stop_background_tasks()

    asyncio.run(run())


def test_dashboard_endpoint_serves_page(tmp_path):
    from fastapi.testclient import TestClient
    from llm_router.main import app

    with TestClient(app) as client:
        page = client.get("/dashboard")
        assert page.status_code in (200, 500)
        if page.status_code == 200:
            assert page.headers["content-type"].startswith("text/html")