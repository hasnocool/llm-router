# tests/test_zero_cost_router.py
from __future__ import annotations

import httpx

from llm_router.config import ModelRoute, ProviderConfig, Settings
from llm_router.providers.base import QuotaExceededError
from llm_router.schemas import ChatRequest, Message
from llm_router.zero_cost_router import ZeroCostModelRouter


def make_settings() -> Settings:
    return Settings(
        strategy="zero-cost",
        providers={
            "huggingface": ProviderConfig(
                name="HF", base_url="https://hf.example", default_model="hf-default"
            ),
            "groq": ProviderConfig(
                name="Groq", base_url="https://groq.example", default_model="groq-default"
            ),
            "local": ProviderConfig(
                name="Local", base_url="http://localhost:8081", default_model="granite"
            ),
        },
        models={
            "cloud-alias": ModelRoute(provider="huggingface", model="Qwen/Qwen3-8B"),
        },
    )


def request() -> ChatRequest:
    return ChatRequest(
        model="cloud-alias",
        messages=[Message(role="user", content="hi")],
    )


def ok_body(model: str, content: str) -> dict:
    return {
        "id": "x",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class QuotaProvider:
    async def complete(self, payload):
        raise QuotaExceededError("quota exhausted", "groq")


class WorkingProvider:
    def __init__(self, content: str):
        self.content = content

    async def complete(self, payload):
        return ok_body(payload["model"], self.content)


async def test_zero_cost_order_prefers_best_recurring_route() -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(make_settings(), client)
        assert router._order(request()) == [
            ("groq", "groq-default"),
            ("huggingface", "Qwen/Qwen3-8B"),
            ("local", "granite"),
        ]


async def test_quota_exhaustion_fails_over_and_demotes_route() -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(make_settings(), client)
        router.providers["groq"] = QuotaProvider()
        router.providers["huggingface"] = WorkingProvider("from-hf")
        router.providers["local"] = WorkingProvider("from-local")

        response = await router.complete(request())
        assert response.provider == "huggingface"
        assert response.choices[0].message.content == "from-hf"
        assert "groq" not in [provider for provider, _ in router._order(request())]


async def test_zero_cost_auto_uses_best_eligible_default_model() -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(make_settings(), client)
        order = router._order(ChatRequest(model="auto", messages=[Message(role="user", content="hi")]))
        assert order == [
            ("groq", "groq-default"),
            ("huggingface", "hf-default"),
            ("local", "granite"),
        ]


async def test_zero_cost_auto_respects_routing_pool() -> None:
    async with httpx.AsyncClient() as client:
        settings = make_settings()
        settings.routing_providers = ["huggingface"]
        router = ZeroCostModelRouter(settings, client)
        order = router._order(ChatRequest(model="auto", messages=[Message(role="user", content="hi")]))
        assert order == [("huggingface", "hf-default")]
