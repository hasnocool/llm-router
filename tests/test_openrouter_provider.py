# tests/test_openrouter_provider.py
from __future__ import annotations

import httpx

from llm_router.config import load_settings
from llm_router.providers import PROVIDER_CLASSES, build_provider
from llm_router.providers.base import Provider


def test_openrouter_is_registered_and_uses_openai_compatibility(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """
strategy = "cloud-first"

[providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
default_model = "openrouter/free"
api_key_env = "OPENROUTER_API_KEY"

[routing]
providers = ["openrouter"]
""",
        encoding="utf-8",
    )
    settings = load_settings(config_path=config, env={"OPENROUTER_API_KEY": "test-key"})
    assert settings.providers["openrouter"].api_key == "test-key"
    assert settings.providers["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert settings.providers["openrouter"].default_model == "openrouter/free"
    assert PROVIDER_CLASSES["openrouter"] is not None

    async def run() -> None:
        async with httpx.AsyncClient() as client:
            provider = build_provider("openrouter", settings.providers["openrouter"], client)
            assert isinstance(provider, Provider)
            headers = provider._headers()
            assert headers["Authorization"] == "Bearer test-key"
            assert headers["HTTP-Referer"] == "https://llm-router.local"
            assert headers["X-Title"] == "llm-router"

    import asyncio

    asyncio.run(run())
