from __future__ import annotations

import httpx

from ..config import ProviderConfig
from ..metrics_db import get_metrics_db
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


_metrics_db = None


def set_metrics_db(db):
    global _metrics_db
    _metrics_db = db


def build_provider(name: str, config: ProviderConfig, http: httpx.AsyncClient) -> object:
    cls = PROVIDER_CLASSES.get(name)
    if cls is None:
        raise ValueError(f"no provider class registered for {name!r}")
    return cls(name=name, config=config, http=http, metrics_db=_metrics_db)