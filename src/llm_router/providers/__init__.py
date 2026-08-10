# src/llm_router/providers/__init__.py
from __future__ import annotations

import httpx

from ..async_metrics import AsyncMetricsStore
from ..config import ProviderConfig
from .base import Provider
from .google_ai import GoogleAIProvider

PROVIDER_CLASSES: dict[str, type] = {
    "huggingface": Provider,
    "local": Provider,
    "cerebras": Provider,
    "nvidia": Provider,
    "groq": Provider,
    "google_ai": GoogleAIProvider,
}

_metrics_store: AsyncMetricsStore | None = None


def set_metrics_store(store: AsyncMetricsStore) -> None:
    global _metrics_store
    _metrics_store = store


def set_metrics_db(store: AsyncMetricsStore) -> None:
    """Backward-compatible alias retained for older imports."""
    set_metrics_store(store)


def build_provider(name: str, config: ProviderConfig, http: httpx.AsyncClient) -> object:
    cls = PROVIDER_CLASSES.get(name)
    if cls is None:
        raise ValueError(f"no provider class registered for {name!r}")
    return cls(name=name, config=config, http=http, metrics_store=_metrics_store)
