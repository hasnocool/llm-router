from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from ..config import ProviderConfig

# HTTP statuses that indicate the provider is unavailable (retryable/failover).
# Anything else (400/401/403/404/422) is a client error and never triggers failover.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


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


class Provider:
    def __init__(self, name: str, config: ProviderConfig, http: httpx.AsyncClient):
        self.name = name
        self.config = config
        self._http = http

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
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

    async def models(self) -> list[dict[str, Any]]:
        resp = await self._http.get(self._url("models"), headers=self._headers())
        self._check_status(resp)
        data = resp.json()
        return data.get("data", [])

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Non-streaming chat completion. Returns the provider's JSON response."""
        try:
            resp = await self._http.post(
                self._url("chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=self.config.timeout_seconds,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise ProviderUnavailable(f"{self.name} unreachable: {exc}") from exc
        self._check_status(resp)
        return resp.json()

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        """Streaming chat completion. Yields raw SSE lines (including blank lines)."""
        emitted = False
        try:
            async with self._http.stream(
                "POST",
                self._url("chat/completions"),
                headers=self._headers(),
                json=payload,
                timeout=self.config.stream_timeout_seconds,
            ) as resp:
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
                    yield line
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            if emitted:
                raise
            raise ProviderUnavailable(f"{self.name} unreachable during stream: {exc}") from exc
