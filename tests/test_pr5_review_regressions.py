# tests/test_pr5_review_regressions.py
from __future__ import annotations

import httpx
import pytest

from llm_router.config import ConfigError, ModelRoute, ProviderConfig, Settings
from llm_router.schemas import ChatRequest, Message
from llm_router.zero_cost_router import ZeroCostModelRouter


def provider(name: str, model: str) -> ProviderConfig:
    return ProviderConfig(name=name, base_url=f"https://{name}.example", default_model=model)


def test_bare_provider_id_resolves_to_default_model() -> None:
    settings = Settings(
        providers={
            "huggingface": provider("HF", "hf-default"),
            "groq": provider("Groq", "groq-default"),
        }
    )

    assert settings.resolve("groq") == ("groq", "groq-default")
    assert settings.resolve("huggingface") == ("huggingface", "hf-default")


def test_model_alias_cannot_shadow_provider_id() -> None:
    with pytest.raises(ConfigError, match="must not shadow provider ids"):
        Settings(
            providers={"groq": provider("Groq", "groq-default")},
            models={"groq": ModelRoute(provider="groq", model="another-model")},
        )


async def test_auto_excludes_ineligible_exhausted_route() -> None:
    settings = Settings(
        strategy="zero-cost",
        providers={
            "groq": provider("Groq", "groq-default"),
            "huggingface": provider("HF", "hf-default"),
            "local": provider("Local", "granite"),
        },
    )
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(settings, client)
        router.status["groq"].daily_calls_used = 100
        router.status["groq"].daily_calls_remaining = 0

        order = router._order(
            ChatRequest(
                model="auto",
                messages=[Message(role="user", content="hi")],
            )
        )

    assert "groq" not in [name for name, _ in order]
    assert ("huggingface", "hf-default") in order
    assert ("local", "granite") in order
