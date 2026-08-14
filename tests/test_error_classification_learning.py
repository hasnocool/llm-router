from __future__ import annotations

import time

import httpx
import pytest

from llm_router.config import MetricsConfig, ProviderConfig, Settings
from llm_router.providers.base import (
    ERROR_AUTHENTICATION,
    ERROR_BILLING_OR_QUOTA,
    ERROR_CONTEXT_LIMIT,
    ERROR_MODEL_UNAVAILABLE,
    ERROR_PROVIDER_UNAVAILABLE,
    ERROR_RATE_LIMITED,
    ERROR_REQUEST_INCOMPATIBLE,
    Provider,
    ProviderRequestError,
    ProviderUnavailable,
    classify_provider_error,
)
from llm_router.schemas import ChatRequest, Message
from llm_router.task_classifier import TaskProfile
from llm_router.zero_cost_router import ZeroCostModelRouter


def settings(tmp_path, providers=None) -> Settings:
    provider_map = providers or {
        "openrouter": ProviderConfig("OpenRouter", "https://openrouter.example", "paid-model"),
        "groq": ProviderConfig("Groq", "https://groq.example", "groq-default"),
    }
    return Settings(
        strategy="zero-cost",
        providers=provider_map,
        routing_providers=list(provider_map),
        metrics=MetricsConfig(db_path=str(tmp_path / "metrics.db"), report_interval_seconds=0),
    )


def request(stream=False) -> ChatRequest:
    return ChatRequest(model="auto", messages=[Message(role="user", content="write code")], stream=stream)


def ok(model):
    return {
        "id": "x", "created": 1, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.mark.parametrize(("status", "expected"), [
    (400, ERROR_REQUEST_INCOMPATIBLE),
    (422, ERROR_REQUEST_INCOMPATIBLE),
    (401, ERROR_AUTHENTICATION),
    (403, ERROR_AUTHENTICATION),
    (402, ERROR_BILLING_OR_QUOTA),
    (404, ERROR_MODEL_UNAVAILABLE),
    (413, ERROR_CONTEXT_LIMIT),
    (429, ERROR_RATE_LIMITED),
    (503, ERROR_PROVIDER_UNAVAILABLE),
    (None, ERROR_PROVIDER_UNAVAILABLE),
])
def test_provider_error_classification(status, expected):
    assert classify_provider_error(status) == expected


@pytest.mark.asyncio
async def test_check_status_preserves_upstream_detail_for_400_and_402(tmp_path):
    async with httpx.AsyncClient() as client:
        provider = Provider("demo", ProviderConfig("Demo", "https://demo.example", "m"), client)
        for status, error_type, error_class in [
            (400, ProviderRequestError, ERROR_REQUEST_INCOMPATIBLE),
            (402, ProviderUnavailable, ERROR_BILLING_OR_QUOTA),
        ]:
            response = httpx.Response(status, json={"error": {"message": "tool choice unsupported"}})
            with pytest.raises(error_type) as caught:
                await provider._check_status(response)
            assert "tool choice unsupported" in str(caught.value)
            assert caught.value.error_class == error_class
            assert "tool choice unsupported" in caught.value.body


async def force_order(router, order):
    profile = TaskProfile(kind="coding", confidence=1.0, coding_heavy=True)

    async def ordered(_req):
        return order, profile

    router._order_for_request = ordered


class ScriptedProvider:
    def __init__(self, failures=None):
        self.failures = failures or {}
        self.calls = []

    async def complete(self, payload):
        model = str(payload["model"])
        self.calls.append(model)
        failure = self.failures.get(model)
        if failure:
            raise failure
        return ok(model)


@pytest.mark.asyncio
async def test_400_tries_another_same_provider_model_without_health_penalty(tmp_path):
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(settings(tmp_path), client)
        provider = ScriptedProvider({
            "paid-model": ProviderRequestError("bad tools", status_code=400),
        })
        router.providers["openrouter"] = provider
        router._model_cache["openrouter"] = (time.time(), [{"id": "paid-model"}, {"id": "chat-alt"}])
        router.providers["groq"] = ScriptedProvider()
        await force_order(router, [("openrouter", "paid-model"), ("openrouter", "openrouter/free"), ("groq", "groq-default")])

        response = await router.complete(request())

        assert response.provider == "openrouter"
        assert provider.calls[:2] == ["paid-model", "openrouter/free"]
        assert router.status["openrouter"].consecutive_failures == 0
        await router._metrics_store.close()


@pytest.mark.asyncio
async def test_openrouter_402_falls_back_to_free_route_without_provider_penalty(tmp_path):
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(settings(tmp_path), client)
        provider = ScriptedProvider({
            "paid-model": ProviderUnavailable("payment required", status_code=402),
        })
        router.providers["openrouter"] = provider
        router.providers["groq"] = ScriptedProvider()
        await force_order(router, [("openrouter", "paid-model"), ("openrouter", "openrouter/free"), ("groq", "groq-default")])

        response = await router.complete(request())

        assert response.provider == "openrouter"
        assert response.model == "openrouter/free"
        assert provider.calls == ["paid-model", "openrouter/free"]
        assert router.status["openrouter"].consecutive_failures == 0
        await router._metrics_store.close()


@pytest.mark.asyncio
async def test_auth_failure_disables_provider_without_transient_failure_counter(tmp_path):
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(settings(tmp_path), client)
        first = ScriptedProvider({
            "paid-model": ProviderRequestError("invalid token", status_code=401),
        })
        second = ScriptedProvider()
        router.providers["openrouter"] = first
        router.providers["groq"] = second
        await force_order(router, [("openrouter", "paid-model"), ("openrouter", "openrouter/free"), ("groq", "groq-default")])

        response = await router.complete(request())

        status = router.status["openrouter"]
        assert response.provider == "groq"
        assert status.available is False
        assert status.consecutive_failures == 0
        assert status.last_error_class == ERROR_AUTHENTICATION
        await router._metrics_store.close()


@pytest.mark.asyncio
async def test_repeated_model_task_incompatibility_skips_nonpreferred_alternative(tmp_path):
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(settings(tmp_path), client)
        router._model_cache["groq"] = (time.time(), [
            {"id": "groq-default"}, {"id": "known-bad"}, {"id": "known-good"},
        ])
        await router._metrics_store.run_blocking(lambda db: [
            db.record_router_event(
                provider="groq", model="known-bad", stream=False, explicit=False,
                failover_index=1, success=False, task_kind="coding", request_kind="chat",
                status_code=400, occurred_at=time.time() - offset,
            )
            for offset in (10, 20)
        ])

        expanded = await router._expand_model_fallbacks(
            [("groq", "groq-default")], TaskProfile(kind="coding", confidence=1.0, coding_heavy=True)
        )

        assert ("groq", "known-bad") not in expanded
        assert ("groq", "known-good") in expanded
        await router._metrics_store.close()
