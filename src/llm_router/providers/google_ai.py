from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..config import ProviderConfig
from .base import ProviderRequestError, ProviderUnavailable

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


class GoogleAIProvider:
    """Google AI (Gemini) provider using the generativelanguage REST API."""

    def __init__(self, name: str, config: ProviderConfig, http: httpx.AsyncClient):
        self.name = name
        self.config = config
        self._http = http

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["x-goog-api-key"] = self.config.api_key
        return headers

    def _url(self, path: str) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/{path}"

    def _check(self, resp: httpx.Response) -> None:
        if resp.status_code in RETRYABLE_STATUSES:
            raise ProviderUnavailable(
                f"{self.name} returned HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        if resp.status_code >= 400:
            raise ProviderRequestError(
                f"{self.name} returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:2000],
            )

    def _gemini_to_openai(self, data: dict, model: str) -> dict:
        """Convert Gemini generateContent response to OpenAI chat.completion format."""
        candidates = data.get("candidates", [])
        if not candidates:
            return {
                "id": f"chatcmpl-google-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": data.get("usageMetadata", {}).get("promptTokenCount", 0),
                    "completion_tokens": data.get("usageMetadata", {}).get("candidatesTokenCount", 0),
                    "total_tokens": data.get("usageMetadata", {}).get("totalTokenCount", 0),
                },
            }
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts)
        return {
            "id": f"chatcmpl-google-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": data.get("usageMetadata", {}).get("promptTokenCount", 0),
                "completion_tokens": data.get("usageMetadata", {}).get("candidatesTokenCount", 0),
                "total_tokens": data.get("usageMetadata", {}).get("totalTokenCount", 0),
            },
        }

    def _openai_to_gemini_payload(self, payload: dict) -> dict:
        """Convert OpenAI chat.completion request to Gemini generateContent format."""
        messages = payload.get("messages", [])
        contents = []
        for m in messages:
            role = m.get("role", "user")
            if role == "system":
                role = "user"
            contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
        gemini = {"contents": contents}
        gen_config = {}
        if payload.get("temperature") is not None:
            gen_config["temperature"] = payload["temperature"]
        if payload.get("max_tokens") is not None:
            gen_config["maxOutputTokens"] = payload["max_tokens"]
        if payload.get("top_p") is not None:
            gen_config["topP"] = payload["top_p"]
        if gen_config:
            gemini["generationConfig"] = gen_config
        return gemini

    async def models(self) -> list[dict[str, Any]]:
        resp = await self._http.get(
            self._url("models"),
            headers=self._headers(),
        )
        self._check(resp)
        data = resp.json()
        return [
            {"id": m["name"].removeprefix("models/"), "type": m.get("displayName", "")}
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]

    def _normalize_model(self, model: str) -> str:
        """Ensure model name has the models/ prefix for Gemini API."""
        if not model.startswith("models/"):
            return f"models/{model}"
        return model

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = payload.get("model", self.config.default_model)
        gemini_payload = self._openai_to_gemini_payload(payload)
        api_model = self._normalize_model(model)
        try:
            resp = await self._http.post(
                self._url(f"{api_model}:generateContent"),
                headers=self._headers(),
                json=gemini_payload,
                timeout=self.config.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnavailable(f"{self.name} unreachable: {exc}") from exc
        self._check(resp)
        return self._gemini_to_openai(resp.json(), model)

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        """Streaming via Gemini (not natively SSE-compatible; we buffer the result)."""
        model = payload.get("model", self.config.default_model)
        gemini_payload = self._openai_to_gemini_payload(payload)
        gemini_payload["generationConfig"] = gemini_payload.get("generationConfig", {})
        gemini_payload["generationConfig"]["responseModalities"] = ["TEXT"]
        api_model = self._normalize_model(model)
        try:
            headers = self._headers()
            headers["Accept"] = "text/event-stream"
            resp = await self._http.post(
                self._url(f"{api_model}:streamGenerateContent?alt=sse"),
                headers=headers,
                json=gemini_payload,
                timeout=self.config.stream_timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnavailable(f"{self.name} unreachable during stream: {exc}") from exc
        self._check(resp)
        async for line in resp.aiter_lines():
            yield line
