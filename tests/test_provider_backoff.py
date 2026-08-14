# tests/test_provider_backoff.py
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from llm_router.config import ProviderConfig, Settings
from llm_router.providers.base import ProviderUnavailable
from llm_router.router import (
    PROVIDER_BACKOFF_BASE_SECONDS,
    ModelRouter,
    ProviderStatus,
)
from llm_router.routing_score import ZeroCostPolicy, runtime_route_score
from llm_router.schemas import ChatRequest, Message
from llm_router.zero_cost_router import ZeroCostModelRouter


def _settings(*, strategy: str = "cloud-first") -> Settings:
    return Settings(
        strategy=strategy,
        providers={
            "groq": ProviderConfig("Groq", "https://groq.example", "groq-default"),
            "huggingface": ProviderConfig("HF", "https://hf.example", "hf-default"),
        },
        routing_providers=["groq", "huggingface"],
    )


@pytest.mark.asyncio
async def test_provider_is_blocked_only_after_three_consecutive_failures() -> None:
    async with httpx.AsyncClient() as client:
        router = ModelRouter(_settings(), client)
        status = router.status["groq"]
        status.available = True
        error = ProviderUnavailable("upstream 503", status_code=503)

        with patch("llm_router.router.time.time", return_value=1000.0):
            router._mark_provider_failure("groq", error)
            assert status.available is True
            assert status.consecutive_failures == 1
            assert status.backoff_until == 0.0

            router._mark_provider_failure("groq", error)
            assert status.available is True
            assert status.consecutive_failures == 2

            router._mark_provider_failure("groq", error)
            assert status.available is False
            assert status.consecutive_failures == 3
            assert status.backoff_until == 1000.0 + PROVIDER_BACKOFF_BASE_SECONDS

            router._mark_provider_failure("groq", error)
            assert status.backoff_until == 1000.0 + PROVIDER_BACKOFF_BASE_SECONDS * 2


@pytest.mark.asyncio
async def test_success_resets_failure_count_and_backoff() -> None:
    async with httpx.AsyncClient() as client:
        router = ModelRouter(_settings(), client)
        status = router.status["groq"]
        status.available = False
        status.consecutive_failures = 5
        status.backoff_until = 9999.0

        with patch("llm_router.router.time.time", return_value=1234.0):
            router._mark_provider_success("groq", 42.0)

        assert status.available is True
        assert status.consecutive_failures == 0
        assert status.backoff_until == 0.0
        assert status.latency_ms == 42.0


@pytest.mark.asyncio
async def test_rate_limit_uses_retry_after_immediately() -> None:
    async with httpx.AsyncClient() as client:
        router = ModelRouter(_settings(), client)
        status = router.status["groq"]
        status.available = True
        error = ProviderUnavailable("rate limited", status_code=429, retry_after_until=1500)

        with patch("llm_router.router.time.time", return_value=1000.0):
            router._mark_provider_failure("groq", error)

        assert status.available is False
        assert status.consecutive_failures == 1
        assert status.backoff_until == 1500.0


@pytest.mark.asyncio
async def test_automatic_routing_skips_backoff_but_explicit_provider_can_probe() -> None:
    async with httpx.AsyncClient() as client:
        router = ModelRouter(_settings(), client)
        router.status["groq"].available = False
        router.status["groq"].backoff_until = 1500.0
        auto = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])
        explicit = ChatRequest(model="groq", messages=[Message(role="user", content="hi")])

        with patch("llm_router.router.time.time", return_value=1000.0):
            assert [name for name, _ in router._order(auto)] == ["huggingface"]
            assert router._order(explicit) == [("groq", "groq-default")]

        with patch("llm_router.router.time.time", return_value=1600.0):
            assert "groq" in [name for name, _ in router._order(auto)]


def test_zero_cost_score_retries_provider_after_backoff_expires() -> None:
    status = ProviderStatus(
        name="local", available=False, last_polled=1000.0, backoff_until=1300.0
    )
    policy = ZeroCostPolicy()

    with patch("llm_router.routing_score.time.time", return_value=1200.0):
        blocked = runtime_route_score("local", None, status, policy)
    with patch("llm_router.routing_score.time.time", return_value=1400.0):
        retryable = runtime_route_score("local", None, status, policy)

    assert blocked.eligible is False
    assert "unavailable" in blocked.reasons
    assert retryable.eligible is True
    assert "unavailable" not in retryable.reasons


@pytest.mark.asyncio
async def test_zero_cost_retryable_failure_uses_shared_threshold() -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(_settings(strategy="zero-cost"), client)
        status = router.status["groq"]
        status.available = True
        error = ProviderUnavailable("upstream 503", status_code=503)

        with patch("llm_router.router.time.time", return_value=1000.0):
            router._mark_retryable_failure("groq", error)
            router._mark_retryable_failure("groq", error)
            assert status.available is True
            router._mark_retryable_failure("groq", error)

        assert status.available is False
        assert status.consecutive_failures == 3
