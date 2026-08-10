# src/llm_router/rate_limits.py
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Mapping


_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h)", re.IGNORECASE)


@dataclass
class RateLimitData:
    limit_type: str
    limit_value: int | None
    remaining: int | None
    reset_timestamp: int | None
    header_source: str


def _safe_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.strip()))
    except (TypeError, ValueError):
        return None


def parse_reset_timestamp(value: str | None, *, now: float | None = None) -> int | None:
    """Parse epoch, seconds-from-now, duration strings, or HTTP dates defensively."""
    if not value:
        return None
    now_value = now if now is not None else time.time()
    text = value.strip()

    numeric = _safe_int(text)
    if numeric is not None:
        return numeric if numeric > 100_000_000 else int(now_value + max(0, numeric))

    total_seconds = 0.0
    matches = list(_DURATION_RE.finditer(text))
    if matches and "".join(match.group(0) for match in matches).lower() == text.lower():
        multipliers = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
        for match in matches:
            total_seconds += float(match.group("value")) * multipliers[match.group("unit").lower()]
        return int(now_value + total_seconds)

    try:
        return int(parsedate_to_datetime(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def retry_after_timestamp(headers: Mapping[str, str]) -> int | None:
    return parse_reset_timestamp(headers.get("retry-after"))


class RateLimitParser:
    """Parse rate-limit headers without allowing telemetry errors to break requests."""

    @staticmethod
    def _build(
        *,
        limit_type: str,
        remaining: str | None,
        limit: str | None,
        reset: str | None,
        source: str,
    ) -> RateLimitData | None:
        parsed_remaining = _safe_int(remaining)
        if parsed_remaining is None:
            return None
        return RateLimitData(
            limit_type=limit_type,
            limit_value=_safe_int(limit),
            remaining=parsed_remaining,
            reset_timestamp=parse_reset_timestamp(reset),
            header_source=source,
        )

    @classmethod
    def parse_openai_compatible(cls, headers: Mapping[str, str]) -> list[RateLimitData]:
        results: list[RateLimitData] = []
        request_limit = cls._build(
            limit_type="requests",
            remaining=headers.get("x-ratelimit-remaining-requests"),
            limit=headers.get("x-ratelimit-limit-requests"),
            reset=headers.get("x-ratelimit-reset-requests") or headers.get("x-ratelimit-reset"),
            source="x-ratelimit-remaining-requests",
        )
        token_limit = cls._build(
            limit_type="tokens",
            remaining=headers.get("x-ratelimit-remaining-tokens"),
            limit=headers.get("x-ratelimit-limit-tokens"),
            reset=headers.get("x-ratelimit-reset-tokens") or headers.get("x-ratelimit-reset"),
            source="x-ratelimit-remaining-tokens",
        )
        if request_limit:
            results.append(request_limit)
        if token_limit:
            results.append(token_limit)
        return results

    @classmethod
    def parse_single_window(cls, headers: Mapping[str, str]) -> list[RateLimitData]:
        item = cls._build(
            limit_type="requests",
            remaining=headers.get("x-ratelimit-remaining") or headers.get("ratelimit-remaining"),
            limit=headers.get("x-ratelimit-limit") or headers.get("ratelimit-limit"),
            reset=headers.get("x-ratelimit-reset") or headers.get("ratelimit-reset"),
            source=(
                "x-ratelimit-remaining"
                if headers.get("x-ratelimit-remaining") is not None
                else "ratelimit-remaining"
            ),
        )
        return [item] if item else []

    @classmethod
    def parse_for_provider(cls, provider: str, headers: Mapping[str, str]) -> list[RateLimitData]:
        if provider in {"nvidia", "cerebras", "groq", "local"}:
            return cls.parse_openai_compatible(headers)
        return cls.parse_single_window(headers)


def extract_usage_from_response(data: dict[str, Any]) -> tuple[int, int]:
    usage = data.get("usage", {})
    return int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)


def extract_usage_from_streaming_chunk(line: str) -> tuple[int, int] | None:
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    usage = obj.get("usage")
    if usage:
        return int(usage.get("prompt_tokens", 0) or 0), int(
            usage.get("completion_tokens", 0) or 0
        )
    return None
