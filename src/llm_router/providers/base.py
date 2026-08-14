# src/llm_router/providers/base.py
from __future__ import annotations

import json
import time
from contextvars import ContextVar, Token
from typing import Any, AsyncIterator

import httpx

from ..async_metrics import AsyncMetricsStore
from ..config import ProviderConfig
from ..metrics_db import QuotaLimitExceeded
from ..rate_limits import RateLimitParser, extract_usage_from_response, retry_after_timestamp

# HTTP statuses that indicate the provider is unavailable (retryable/failover).
# 402 (payment required) is included so auto/fallback routing skips providers
# whose billing/quota prevents them from serving. 413 (payload too large) is
# also retryable because context/payload limits are provider-specific: a prompt
# rejected by one provider may fit another, so zero-cost routing fails over.
# Anything else (400/401/403/404/422) is a client error and never triggers failover.
RETRYABLE_STATUSES = {402, 413, 429, 500, 502, 503, 504}

FORWARDED_REQUEST_HEADERS: ContextVar[dict[str, str]] = ContextVar(
    "forwarded_request_headers", default={}
)
FORWARDED_HEADER_ALLOWLIST = {
    "x-session-id",
    "x-client-request-id",
    "session_id",
    "x-session-affinity",
    "prompt_cache_key",
    "anthropic-beta",
    "anthropic-version",
    "cache-control",
}


def set_forwarded_request_headers(headers: dict[str, str]) -> Token:
    forwarded = {
        name.lower(): value
        for name, value in headers.items()
        if name and name.lower() in FORWARDED_HEADER_ALLOWLIST and value
    }
    return FORWARDED_REQUEST_HEADERS.set(forwarded)


def reset_forwarded_request_headers(token: Token) -> None:
    FORWARDED_REQUEST_HEADERS.reset(token)


def get_forwarded_request_headers() -> dict[str, str]:
    return FORWARDED_REQUEST_HEADERS.get()


class ProviderUnavailable(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retry_after_until: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_until = retry_after_until


class ProviderRequestError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class QuotaExceededError(ProviderRequestError):
    def __init__(self, message: str, provider: str):
        super().__init__(message, status_code=429)
        self.provider = provider


def estimate_request_tokens(payload: dict[str, Any]) -> int:
    chars = 0
    for message in payload.get("messages", []):
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chars += len(part["text"])
    input_estimate = max(1, chars // 4) if chars else 0
    output_estimate = int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 0)
    return input_estimate + max(0, output_estimate)


def classify_request_kind(payload: dict[str, Any]) -> str:
    tool_choice = payload.get("tool_choice")
    if tool_choice == "none":
        return "chat"
    if payload.get("tools") or tool_choice:
        return "tool_call"
    return "chat"


def classify_response_kind(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    if message.get("tool_calls"):
        return "tool_call"
    if choice.get("finish_reason") == "tool_calls":
        return "tool_call"
    return "chat"


class Provider:
    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        http: httpx.AsyncClient,
        metrics_store: AsyncMetricsStore | None = None,
        metrics_db: AsyncMetricsStore | None = None,
    ):
        self.name = name
        self.config = config
        self._http = http
        self._metrics_store = metrics_store or metrics_db

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(get_forwarded_request_headers())
        return headers

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}/{path.lstrip('/')}"

    async def _check_status(self, resp: httpx.Response) -> None:
        if resp.status_code in RETRYABLE_STATUSES:
            raise ProviderUnavailable(
                f"{self.name} returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                retry_after_until=retry_after_timestamp(resp.headers),
            )
        if resp.status_code >= 400:
            try:
                await resp.aread()
                body = resp.text[:2000]
            except (httpx.HTTPError, httpx.StreamError, UnicodeError):
                body = ""
            raise ProviderRequestError(
                f"{self.name} returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=body,
            )

    async def _reserve_quota(self, payload: dict[str, Any]) -> str | None:
        if self._metrics_store is None:
            return None
        try:
            return await self._metrics_store.reserve_quota(
                self.name, estimated_tokens=estimate_request_tokens(payload)
            )
        except QuotaLimitExceeded as exc:
            raise QuotaExceededError(f"{self.name}: {exc}", self.name) from exc

    async def _record_rate_limits(self, headers: dict[str, str]) -> None:
        if self._metrics_store is None:
            return
        await self._metrics_store.upsert_rate_limits(
            self.name, RateLimitParser.parse_for_provider(self.name, headers)
        )

    async def _record_attempt(
        self,
        *,
        reservation_id: str | None,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
        status_code: int | None = None,
        request_kind: str = "chat",
        response_kind: str = "",
    ) -> None:
        if self._metrics_store is None:
            return
        await self._metrics_store.reconcile_reservation(
            reservation_id,
            self.name,
            success,
            prompt_tokens,
            completion_tokens,
            latency_ms,
            status_code,
            request_kind,
            response_kind,
        )

    async def models(self) -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        status_code: int | None = None
        try:
            resp = await self._http.get(
                self._url("models"), headers=self._headers(), timeout=self.config.timeout_seconds
            )
            status_code = resp.status_code
            await self._record_rate_limits(dict(resp.headers))
            await self._check_status(resp)
            data = resp.json()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            await self._record_attempt(
                reservation_id=None,
                success=False,
                latency_ms=(time.perf_counter() - t0) * 1000,
                status_code=status_code,
                request_kind="model_discovery",
            )
            raise ProviderUnavailable(f"{self.name} unreachable: {exc}") from exc
        except (ProviderUnavailable, ProviderRequestError):
            await self._record_attempt(
                reservation_id=None,
                success=False,
                latency_ms=(time.perf_counter() - t0) * 1000,
                status_code=status_code,
                request_kind="model_discovery",
            )
            raise

        await self._record_attempt(
            reservation_id=None,
            success=True,
            latency_ms=(time.perf_counter() - t0) * 1000,
            status_code=status_code,
            request_kind="model_discovery",
        )
        return data.get("data", [])

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        reservation_id = await self._reserve_quota(payload)
        request_kind = classify_request_kind(payload)
        t0 = time.perf_counter()
        status_code: int | None = None
        try:
            resp = await self._http.post(
                self._url("chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=self.config.timeout_seconds,
            )
            status_code = resp.status_code
            await self._record_rate_limits(dict(resp.headers))
            await self._check_status(resp)
            data = resp.json()
            prompt_tokens, completion_tokens = extract_usage_from_response(data)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            await self._record_attempt(
                reservation_id=reservation_id,
                success=False,
                latency_ms=(time.perf_counter() - t0) * 1000,
                status_code=status_code,
                request_kind=request_kind,
            )
            raise ProviderUnavailable(f"{self.name} unreachable: {exc}") from exc
        except (ProviderUnavailable, ProviderRequestError):
            await self._record_attempt(
                reservation_id=reservation_id,
                success=False,
                latency_ms=(time.perf_counter() - t0) * 1000,
                status_code=status_code,
                request_kind=request_kind,
            )
            raise

        await self._record_attempt(
            reservation_id=reservation_id,
            success=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=(time.perf_counter() - t0) * 1000,
            status_code=status_code,
            request_kind=request_kind,
            response_kind=classify_response_kind(data),
        )
        return data

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        reservation_id = await self._reserve_quota(payload)
        request_kind = classify_request_kind(payload)
        t0 = time.perf_counter()
        emitted = False
        prompt_tokens = 0
        completion_tokens = 0
        status_code: int | None = None
        recorded = False
        tool_response = False
        try:
            async with self._http.stream(
                "POST",
                self._url("chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=self.config.stream_timeout_seconds,
            ) as resp:
                status_code = resp.status_code
                await self._record_rate_limits(dict(resp.headers))
                await self._check_status(resp)
                async for line in resp.aiter_lines():
                    if line:
                        emitted = True
                    usage = self._extract_streaming_usage(line)
                    if usage:
                        prompt_tokens = max(prompt_tokens, usage[0])
                        completion_tokens = max(completion_tokens, usage[1])
                    if self._stream_chunk_has_tool_calls(line):
                        tool_response = True
                    yield line
            await self._record_attempt(
                reservation_id=reservation_id,
                success=True,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=(time.perf_counter() - t0) * 1000,
                status_code=status_code,
                request_kind=request_kind,
                response_kind="tool_call" if tool_response else "chat",
            )
            recorded = True
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            await self._record_attempt(
                reservation_id=reservation_id,
                success=False,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=(time.perf_counter() - t0) * 1000,
                status_code=status_code,
                request_kind=request_kind,
            )
            recorded = True
            if emitted:
                raise
            raise ProviderUnavailable(f"{self.name} unreachable during stream: {exc}") from exc
        except (ProviderUnavailable, ProviderRequestError):
            await self._record_attempt(
                reservation_id=reservation_id,
                success=False,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=(time.perf_counter() - t0) * 1000,
                status_code=status_code,
                request_kind=request_kind,
            )
            recorded = True
            raise
        finally:
            if not recorded and self._metrics_store is not None:
                await self._metrics_store.cancel_reservation(reservation_id)

    @staticmethod
    def _stream_chunk_has_tool_calls(line: str) -> bool:
        if not line.startswith("data:"):
            return False
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return False
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return False
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("tool_calls"):
                return True
        return False

    @staticmethod
    def _extract_streaming_usage(line: str) -> tuple[int, int] | None:
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return None
        usage = obj.get("usage")
        if not usage:
            return None
        return int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)
