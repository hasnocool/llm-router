# tests/test_task_classifier.py
from __future__ import annotations

import httpx

from llm_router.config import ProviderConfig, Settings
from llm_router.schemas import ChatRequest, Message
from llm_router.task_classifier import classify_task, TaskProfile
from llm_router.zero_cost_router import ZeroCostModelRouter


def test_classify_task_detects_coding_and_tools():
    profile = classify_task(
        {
            "messages": [{"role": "user", "content": "Fix this Python bug and call a tool"}],
            "tools": [{"type": "function"}],
        }
    )
    assert profile.kind == "tool_use"
    assert profile.confidence >= 0.9
    assert profile.coding_heavy is True


def test_classify_task_detects_structured_output():
    profile = classify_task({"messages": [{"role": "user", "content": "Return JSON schema"}]})
    assert profile.kind == "structured"
    assert profile.needs_json is True


def test_zero_cost_routing_prefers_free_tool_or_coding_capable_routes():
    settings = Settings(
        providers={
            "groq": ProviderConfig("Groq", "https://groq.example", "groq-default"),
            "huggingface": ProviderConfig("HF", "https://hf.example", "hf-default"),
            "openrouter": ProviderConfig("OpenRouter", "https://openrouter.ai/api/v1", "openrouter/free"),
            "local": ProviderConfig("Local", "http://localhost:8081", "granite"),
        }
    )
    async def run():
        async with httpx.AsyncClient() as client:
            router = ZeroCostModelRouter(settings, client)
            order = router._order(
                ChatRequest(
                    model="auto",
                    messages=[Message(role="user", content="Fix this Python stack trace and write code")],
                )
            )
            assert order
            assert order[0][0] in {"groq", "huggingface", "openrouter", "local"}

    import asyncio

    asyncio.run(run())


def test_low_confidence_can_trigger_model_fallback(monkeypatch):
    settings = Settings(
        strategy="zero-cost",
        providers={
            "groq": ProviderConfig("Groq", "https://groq.example", "groq-default"),
            "openrouter": ProviderConfig("OpenRouter", "https://openrouter.ai/api/v1", "openrouter/free", api_key="test-key"),
        }
    )

    async def run():
        async with httpx.AsyncClient() as client:
            router = ZeroCostModelRouter(settings, client)
            called = {"count": 0}

            async def fake_refine(settings_arg, payload, base):
                called["count"] += 1
                return TaskProfile(kind="coding", confidence=0.99, coding_heavy=True)

            monkeypatch.setattr("llm_router.zero_cost_router.refine_task_profile_with_model", fake_refine)
            monkeypatch.setattr("llm_router.zero_cost_router.classify_task", lambda payload: TaskProfile(kind="general", confidence=0.2))
            order = await router._order_for_request(ChatRequest(model="auto", messages=[Message(role="user", content="hello")]))
            assert called["count"] == 1
            assert order[0][0] in {"groq", "openrouter"}

    import asyncio

    asyncio.run(run())
