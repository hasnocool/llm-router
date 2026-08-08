from __future__ import annotations

import httpx

from ..config import ProviderConfig
from .base import Provider


class LocalProvider(Provider):
    """Local self-hosted model served by llama.cpp's llama-server (OpenAI-compatible)."""

    def __init__(self, config: ProviderConfig, http: httpx.AsyncClient):
        super().__init__(name="local", config=config, http=http)
