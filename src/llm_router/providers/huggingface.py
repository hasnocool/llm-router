from __future__ import annotations

import httpx

from ..config import ProviderConfig
from .base import Provider


class HuggingFaceProvider(Provider):
    """HuggingFace Serverless Inference via the OpenAI-compatible router."""

    def __init__(self, config: ProviderConfig, http: httpx.AsyncClient):
        super().__init__(name="huggingface", config=config, http=http)
