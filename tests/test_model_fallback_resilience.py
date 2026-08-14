from __future__ import annotations

import time

import httpx
import pytest

from llm_router.config import MetricsConfig, ProviderConfig, Settings
from llm_router.providers.base import ProviderRequestError, ProviderUnavailable
from llm_router.schemas import ChatRequest, Message
from llm_router.task_classifier import TaskProfile
from llm_router.zero_cost_router import ZeroCostModelRouter


def _settings(tmp_path) -> Settings:
    return Settings(
        strategy="zero-cost",
        providers={
            "groq": ProviderConfig("Groq", "https://groq.example", "groq-default"),
            "huggingface": ProviderConfig("HF", "https://hf.example", "hf-default"),
        },
        routing_providers=["groq", "huggingface"],
        metrics=MetricsConfig(
            db_path=str(tmp_path / "metrics.db"),
            report_interval_seconds=0,
        ),
    )


def _request(*, stream: bool = False) -> ChatRequest:
    return ChatRequest(
        model="auto",
        messages=[Message(role="user", content="write a small parser")],
        stream=stream,
    )


def _ok(model: str, text: str = "ok") -> dict:
    return {
        "id": "x",
        "created": 1,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class ModelFallbackProvider:
    def __init__(self, *, missing: set[str] | None = None, unavailable: bool = False) -> None:
        self.missing = missing or set()
        self.unavailable = unavailable
        self.calls: list[str] = []

    async def complete(self, payload):
        model = str(payload["model"])
        self.calls.append(model)
        if self.unavailable:
            raise ProviderUnavailable("temporary upstream failure", status_code=503)
        if model in self.missing:
            raise ProviderRequestError("model not found", status_code=404)
        return _ok(model, f"served by {model}")


class WorkingProvider(ModelFallbackProvider):
    pass


class MidStreamFailureProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, payload):
        self.calls += 1
        yield 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise ProviderUnavailable("stream broke", status_code=503)


class NeverStreamProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, payload):
        self.calls += 1
        yield 'data: {"choices":[{"delta":{"content":"should not appear"}}]}\n\n'


async def test_404_falls_back_to_another_model_on_same_provider(tmp_path) -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(_settings(tmp_path), client)
        groq = ModelFallbackProvider(missing={"groq-default"})
        hf = WorkingProvider()
        router.providers["groq"] = groq
        router.providers["huggingface"] = hf
        router._model_cache["groq"] = (
            time.time(),
            [{"id": "groq-default"}, {"id": "groq-alt"}],
        )

        response = await router.complete(_request())

        assert response.provider == "groq"
        assert response.model == "groq-alt"
        assert groq.calls == ["groq-default", "groq-alt"]
        assert hf.calls == []
        assert router.status["groq"].consecutive_failures == 0
        await router._metrics_store.close()


async def test_other_task_models_remain_in_fallback_catalog(tmp_path) -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(_settings(tmp_path), client)
        router._model_cache["groq"] = (
            time.time(),
            [
                {"id": "groq-default"},
                {"id": "coding-model"},
                {"id": "vision-model"},
            ],
        )
        await router._metrics_store.run_blocking(
            lambda db: (
                db.upsert_provider_models(
                    "groq",
                    [
                        ("coding-model", "discovery"),
                        ("vision-model", "discovery"),
                    ],
                ),
                db.record_router_event(
                    provider="groq",
                    model="coding-model",
                    stream=False,
                    explicit=False,
                    failover_index=0,
                    success=True,
                    request_kind="chat",
                    task_kind="coding",
                ),
                db.record_router_event(
                    provider="groq",
                    model="vision-model",
                    stream=False,
                    explicit=False,
                    failover_index=0,
                    success=True,
                    request_kind="chat",
                    task_kind="vision",
                ),
            )
        )

        expanded = await router._expand_model_fallbacks(
            [("groq", "groq-default")],
            TaskProfile(kind="coding", confidence=1.0, coding_heavy=True),
        )

        assert expanded[0] == ("groq", "groq-default")
        assert ("groq", "coding-model") in expanded
        assert ("groq", "vision-model") in expanded
        assert expanded.index(("groq", "coding-model")) < expanded.index(("groq", "vision-model"))
        await router._metrics_store.close()


async def test_provider_failure_counts_once_even_with_multiple_models(tmp_path) -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(_settings(tmp_path), client)
        groq = ModelFallbackProvider(unavailable=True)
        hf = WorkingProvider()
        router.providers["groq"] = groq
        router.providers["huggingface"] = hf
        router._model_cache["groq"] = (
            time.time(),
            [{"id": "groq-default"}, {"id": "groq-alt-1"}, {"id": "groq-alt-2"}],
        )

        response = await router.complete(_request())

        assert response.provider == "huggingface"
        assert groq.calls == ["groq-default"]
        assert router.status["groq"].consecutive_failures == 1
        await router._metrics_store.close()


async def test_soft_backoff_waits_for_five_provider_failures(tmp_path) -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(_settings(tmp_path), client)
        router._mark_provider_success("groq", 1.0)
        error = ProviderUnavailable("temporary", status_code=503)
        for _ in range(4):
            router._mark_provider_failure("groq", error)
        status = router.status["groq"]
        assert status.available is True
        assert status.backoff_until == 0.0

        before = time.time()
        router._mark_provider_failure("groq", error)
        assert status.available is False
        assert status.backoff_until >= before + 110
        assert status.backoff_until <= before + 130
        await router._metrics_store.close()


async def test_stream_never_fails_over_after_emitting_data(tmp_path) -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(_settings(tmp_path), client)
        first = MidStreamFailureProvider()
        second = NeverStreamProvider()
        router.providers["groq"] = first
        router.providers["huggingface"] = second

        seen: list[str] = []
        with pytest.raises(ProviderUnavailable):
            async for line in router.stream(_request(stream=True)):
                seen.append(line)

        assert any("partial" in line for line in seen)
        assert first.calls == 1
        assert second.calls == 0
        await router._metrics_store.close()
