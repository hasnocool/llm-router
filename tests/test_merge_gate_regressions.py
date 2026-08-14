# tests/test_merge_gate_regressions.py
from __future__ import annotations

from types import MethodType
import json
import logging
import tomllib
from pathlib import Path

import httpx

from llm_router.config import LogsConfig, ProviderConfig, Settings
from llm_router.log_events import APP_EVENT_BUFFER, DBEventLogHandler
from llm_router.metrics_db import MetricsDB
from llm_router.providers.base import classify_request_kind
from llm_router.providers.google_ai import GoogleAIProvider
from llm_router.router import ModelRouter
from llm_router.routing_score import RouteScore
from llm_router.schemas import ChatRequest, Message
from llm_router.task_classifier import TaskProfile
from llm_router.zero_cost_router import ZeroCostModelRouter


def _provider(name: str, model: str) -> ProviderConfig:
    return ProviderConfig(name=name, base_url=f"https://{name}.example", default_model=model)


def test_bare_provider_id_resolves_to_its_default_model() -> None:
    settings = Settings(
        providers={
            "groq": _provider("groq", "groq-default"),
            "huggingface": _provider("huggingface", "hf-default"),
        }
    )
    assert settings.resolve("groq") == ("groq", "groq-default")


def test_tool_choice_none_is_chat_even_when_tools_are_present() -> None:
    assert classify_request_kind({"tools": [{"type": "function"}], "tool_choice": "none"}) == "chat"


def test_gemini_schema_sanitizer_ignores_malformed_properties() -> None:
    cleaned = GoogleAIProvider._sanitize_schema({"type": "object", "properties": "invalid"})
    assert cleaned == {"type": "object"}


async def test_auto_routing_excludes_ineligible_scores() -> None:
    settings = Settings(
        strategy="zero-cost",
        providers={
            "groq": _provider("groq", "groq-default"),
            "huggingface": _provider("huggingface", "hf-default"),
        },
        routing_providers=["groq", "huggingface"],
    )
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(settings, client)

        def fake_scores(self, *, primary=None):
            return [
                RouteScore("groq", None, 99, 99.0, "recurring", False, ("quota-exhausted",)),
                RouteScore("huggingface", None, 80, 80.0, "recurring", True),
            ]

        router.route_scores = MethodType(fake_scores, router)
        req = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])
        profile = TaskProfile(kind="general", confidence=1.0)
        assert router._order_with_profile(req, profile) == [("huggingface", "hf-default")]



def test_default_message_body_logging_is_opt_in() -> None:
    assert LogsConfig().log_message_bodies is False


def test_repository_config_is_valid_toml() -> None:
    parsed = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
    assert parsed["logs"]["log_message_bodies"] is False


def test_gemini_schema_strips_unsupported_composition_keywords() -> None:
    cleaned = GoogleAIProvider._sanitize_schema(
        {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "oneOf": [{"type": "string"}],
                    "allOf": [{"type": "string"}],
                    "not": {"type": "number"},
                    "prefixItems": [{"type": "string"}],
                    "uniqueItems": True,
                    "anyOf": [{"type": "string"}, {"type": "number"}],
                }
            },
        }
    )
    value = cleaned["properties"]["value"]
    assert "oneOf" not in value
    assert "allOf" not in value
    assert "not" not in value
    assert "prefixItems" not in value
    assert "uniqueItems" not in value
    assert "anyOf" in value


def test_sse_tool_call_detection_ignores_non_object_json() -> None:
    assert ModelRouter._sse_has_tool_call("data: []") is False
    assert ModelRouter._sse_has_tool_call("data: 5") is False
    assert ModelRouter._sse_has_tool_call(
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"x"}]}}]}'
    ) is True


def test_truncated_message_body_remains_valid_json() -> None:
    encoded = ModelRouter._json_body({"body": "x" * 1000}, max_chars=80)
    decoded = json.loads(encoded)
    assert len(encoded) <= 80
    assert isinstance(decoded, (dict, int))
    if isinstance(decoded, dict):
        assert decoded["truncated"] is True


def test_event_handler_can_skip_persistence_diagnostics() -> None:
    APP_EVENT_BUFFER.drain()
    handler = DBEventLogHandler()
    record = logging.LogRecord(
        name="llm_router.router",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="db unavailable",
        args=(),
        exc_info=None,
    )
    record.skip_event_buffer = True
    handler.emit(record)
    assert APP_EVENT_BUFFER.drain() == []


def test_provider_model_unknown_limits_stay_null_and_preserve_learned_values(tmp_path) -> None:
    db = MetricsDB(tmp_path / "metrics.db")
    try:
        db.upsert_provider_models("demo", [("m1", "discovery", {"rpm": 10})])
        db.upsert_provider_models("demo", [("m1", "discovery", {})])
        row = db.get_all_provider_models()["demo"][0]
        assert row["rpm"] == 10
        for key in ("max_output_tokens", "rpd", "tpm", "tpd", "rps"):
            assert row[key] is None
        assert row["rate_limit_source"] == ""
        assert row["meta_updated_at"] is not None
    finally:
        db.close()


def test_provider_attempt_metrics_exclude_model_discovery(tmp_path) -> None:
    db = MetricsDB(tmp_path / "metrics.db")
    try:
        db.reconcile_reservation(
            None,
            "demo",
            True,
            request_kind="model_discovery",
        )
        db.reconcile_reservation(
            None,
            "demo",
            True,
            request_kind="chat",
        )
        rows = db.get_provider_attempt_metrics(days=1)
        assert len(rows) == 1
        assert rows[0]["provider_name"] == "demo"
        assert rows[0]["attempts"] == 1
    finally:
        db.close()
