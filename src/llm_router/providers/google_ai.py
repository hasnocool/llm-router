from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..config import ProviderConfig
from ..metrics_db import get_metrics_db
from ..rate_limits import RateLimitParser, extract_usage_from_response
from .base import (
    Provider,
    ProviderRequestError,
    ProviderUnavailable,
    QuotaExceededError,
    RETRYABLE_STATUSES,
    get_forwarded_request_headers,
)


class GoogleAIProvider(Provider):
    """Google AI (Gemini) provider using the generativelanguage REST API."""

    def __init__(
        self, name: str, config: ProviderConfig, http: httpx.AsyncClient, metrics_db=None
    ):
        super().__init__(name, config, http, metrics_db)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["x-goog-api-key"] = self.config.api_key
        headers.update(get_forwarded_request_headers())
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
                    "prompt_tokens": data.get("usageMetadata", {}).get(
                        "promptTokenCount", 0
                    ),
                    "completion_tokens": data.get("usageMetadata", {}).get(
                        "candidatesTokenCount", 0
                    ),
                    "total_tokens": data.get("usageMetadata", {}).get(
                        "totalTokenCount", 0
                    ),
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
                "prompt_tokens": data.get("usageMetadata", {}).get(
                    "promptTokenCount", 0
                ),
                "completion_tokens": data.get("usageMetadata", {}).get(
                    "candidatesTokenCount", 0
                ),
                "total_tokens": data.get("usageMetadata", {}).get(
                    "totalTokenCount", 0
                ),
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

    def _normalize_model(self, model: str) -> str:
        """Ensure model name has the models/ prefix for Gemini API."""
        if not model.startswith("models/"):
            return f"models/{model}"
        return model

    def _extract_gemini_usage(self, data: dict) -> tuple[int, int]:
        """Extract token usage from Gemini response."""
        return (
            data.get("usageMetadata", {}).get("promptTokenCount", 0),
            data.get("usageMetadata", {}).get("candidatesTokenCount", 0),
        )

    async def models(self) -> list[dict[str, Any]]:
        t0 = time.time()
        resp = await self._http.get(
            self._url("models"),
            headers=self._headers(),
        )
        latency_ms = (time.time() - t0) * 1000
        self._record_rate_limits(dict(resp.headers))
        self._check(resp)
        self._record_request_metrics(success=True, latency_ms=latency_ms)
        data = resp.json()
        return [
            {"id": m["name"].removeprefix("models/"), "type": m.get("displayName", "")}
            for m in data.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._check_quota()
        model = payload.get("model", self.config.default_model)
        gemini_payload = self._openai_to_gemini_payload(payload)
        api_model = self._normalize_model(model)
        t0 = time.time()
        try:
            resp = await self._http.post(
                self._url(f"{api_model}:generateContent"),
                headers=self._headers(),
                json=gemini_payload,
                timeout=self.config.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            self._record_request_metrics(success=False, latency_ms=(time.time() - t0) * 1000)
            raise ProviderUnavailable(f"{self.name} unreachable: {exc}") from exc

        latency_ms = (time.time() - t0) * 1000
        self._record_rate_limits(dict(resp.headers))
        self._check(resp)

        data = resp.json()
        prompt_tokens, completion_tokens = self._extract_gemini_usage(data)
        self._record_request_metrics(
            success=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )
        return self._gemini_to_openai(data, model)

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        """Streaming via Gemini (not natively SSE-compatible; we buffer the result)."""
        self._check_quota()
        model = payload.get("model", self.config.default_model)
        gemini_payload = self._openai_to_gemini_payload(payload)
        gemini_payload["generationConfig"] = gemini_payload.get("generationConfig", {})
        gemini_payload["generationConfig"]["responseModalities"] = ["TEXT"]
        api_model = self._normalize_model(model)
        t0 = time.time()
        emitted = False
        total_prompt_tokens = 0
        total_completion_tokens = 0

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
            self._record_request_metrics(
                success=False,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                latency_ms=(time.time() - t0) * 1000,
            )
            raise ProviderUnavailable(f"{self.name} unreachable during stream: {exc}") from exc

        self._record_rate_limits(dict(resp.headers))
        self._check(resp)

        async for line in resp.aiter_lines():
            if not emitted and line:
                emitted = True
            yield line

        self._record_request_metrics(
            success=True,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            latency_ms=(time.time() - t0) * 1000,
        )