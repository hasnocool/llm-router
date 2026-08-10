from __future__ import annotations

import time
from contextvars import ContextVar, Token
from typing import Any, AsyncIterator

import httpx

from ..config import ProviderConfig
from ..metrics_db import get_metrics_db
from ..rate_limits import RateLimitParser, extract_usage_from_response

# HTTP statuses that indicate the provider is unavailable (retryable/failover).
# Anything else (400/401/403/404/422) is a client error and never triggers failover.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

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
    """Provider is down/overloaded — safe to fail over to another provider."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ProviderRequestError(Exception):
    """Provider rejected the request — do NOT fail over."""

    def __init__(self, message: str, status_code: int | None = None, body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class QuotaExceededError(ProviderRequestError):
    """Provider quota exceeded — do NOT fail over, this is a hard limit."""

    def __init__(self, message: str, provider: str):
        super().__init__(message, status_code=429)
        self.provider = provider


class Provider:
    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        http: httpx.AsyncClient,
        metrics_db=None,
    ):
        self.name = name
        self.config = config
        self._http = http
        self._metrics_db = metrics_db or get_metrics_db()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(get_forwarded_request_headers())
        return headers

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}/{path.lstrip('/')}"

    def _check_status(self, resp: httpx.Response) -> None:
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

    def _check_quota(self) -> None:
        """Check if daily quota is exceeded before making request."""
        exceeded, msg = self._metrics_db.check_quota_exceeded(self.name)
        if exceeded:
            raise QuotaExceededError(f"{self.name}: {msg}", self.name)

    def _record_rate_limits(self, headers: dict[str, str]) -> None:
        """Parse and store rate limit headers from response."""
        for rl in RateLimitParser.parse_for_provider(self.name, headers):
            self._metrics_db.upsert_rate_limit(
                provider=self.name,
                limit_type=rl.limit_type,
                limit_value=rl.limit_value,
                remaining=rl.remaining,
                reset_timestamp=rl.reset_timestamp,
                header_source=rl.header_source,
            )

    def _record_request_metrics(
        self,
        success: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        latency_ms: float = 0.0,
    ) -> None:
        """Record request metrics to database."""
        self._metrics_db.record_request(
            provider=self.name,
            success=success,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )

    async def models(self) -> list[dict[str, Any]]:
        t0 = time.time()
        resp = await self._http.get(self._url("models"), headers=self._headers())
        latency_ms = (time.time() - t0) * 1000
        self._record_rate_limits(dict(resp.headers))
        self._check_status(resp)
        self._record_request_metrics(success=True, latency_ms=latency_ms)
        data = resp.json()
        return data.get("data", [])

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Non-streaming chat completion. Returns the provider's JSON response."""
        self._check_quota()
        t0 = time.time()
        try:
            resp = await self._http.post(
                self._url("chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=self.config.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            self._record_request_metrics(success=False, latency_ms=(time.time() - t0) * 1000)
            raise ProviderUnavailable(f"{self.name} unreachable: {exc}") from exc

        latency_ms = (time.time() - t0) * 1000
        self._record_rate_limits(dict(resp.headers))
        self._check_status(resp)

        data = resp.json()
        prompt_tokens, completion_tokens = extract_usage_from_response(data)
        self._record_request_metrics(
            success=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        )
        return data

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        """Streaming chat completion. Yields raw SSE lines (including blank lines)."""
        self._check_quota()
        t0 = time.time()
        emitted = False
        total_prompt_tokens = 0
        total_completion_tokens = 0

        try:
            async with self._http.stream(
                "POST",
                self._url("chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=self.config.stream_timeout_seconds,
            ) as resp:
                self._record_rate_limits(dict(resp.headers))

                if resp.status_code in RETRYABLE_STATUSES:
                    raise ProviderUnavailable(
                        f"{self.name} returned HTTP {resp.status_code}",
                        status_code=resp.status_code,
                    )
                if resp.status_code >= 400:
                    body = await resp.aread()
                    raise ProviderRequestError(
                        f"{self.name} returned HTTP {resp.status_code}",
                        status_code=resp.status_code,
                        body=body.decode("utf-8", "replace")[:2000],
                    )

                async for line in resp.aiter_lines():
                    if not emitted and line:
                        emitted = True
                        latency_ms = (time.time() - t0) * 1000

                    # Extract token usage from streaming chunks
                    usage = self._extract_streaming_usage(line)
                    if usage:
                        total_prompt_tokens += usage[0]
                        total_completion_tokens += usage[1]

                    yield line

                # Record metrics after successful stream completion
                self._record_request_metrics(
                    success=True,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    latency_ms=(time.time() - t0) * 1000,
                )

        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            latency_ms = (time.time() - t0) * 1000
            self._record_request_metrics(
                success=False,
                prompt_tokens=total_prompt_tokens,
                completion_tokens=total_completion_tokens,
                latency_ms=latency_ms,
            )
            if emitted:
                raise
            raise ProviderUnavailable(
                f"{self.name} unreachable during stream: {exc}"
            ) from exc

    def _extract_streaming_usage(self, line: str) -> tuple[int, int] | None:
        """Extract token usage from streaming SSE line."""
        if not line.startswith("data:"):
            return None
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            import json

            obj = json.loads(payload)
            usage = obj.get("usage")
            if usage:
                return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        except Exception:
            pass
        return None