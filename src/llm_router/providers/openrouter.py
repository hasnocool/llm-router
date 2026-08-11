# src/llm_router/providers/openrouter.py
from __future__ import annotations

import httpx

from ..async_metrics import AsyncMetricsStore
from ..config import ProviderConfig
from .base import Provider


class OpenRouterProvider(Provider):
    """OpenRouter's OpenAI-compatible API."""

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        http: httpx.AsyncClient,
        metrics_store: AsyncMetricsStore | None = None,
        metrics_db: AsyncMetricsStore | None = None,
    ):
        super().__init__(name, config, http, metrics_store=metrics_store, metrics_db=metrics_db)

    def _headers(self) -> dict[str, str]:
        headers = super()._headers()
        headers.setdefault("HTTP-Referer", "https://llm-router.local")
        headers.setdefault("X-Title", "llm-router")
        return headers
