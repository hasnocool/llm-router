# tests/test_merge_gate_regressions.py
from __future__ import annotations

from types import MethodType

import httpx

from llm_router.config import ProviderConfig, Settings
from llm_router.providers.base import classify_request_kind
from llm_router.providers.google_ai import GoogleAIProvider
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
