from __future__ import annotations

import httpx

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


def build_provider(name: str, config: ProviderConfig, http: httpx.AsyncClient) -> object:
    cls = PROVIDER_CLASSES.get(name)
    if cls is None:
        raise ValueError(f"no provider class registered for {name!r}")
    return cls(name=name, config=config, http=http)
