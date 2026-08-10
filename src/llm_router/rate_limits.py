from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class RateLimitData:
    limit_type: str
    limit_value: int | None
    remaining: int | None
    reset_timestamp: int | None
    header_source: str


class RateLimitParser:
    """Parse rate limit headers from different provider responses."""

    @staticmethod
    def parse_openai_compatible(headers: dict[str, str]) -> list[RateLimitData]:
        """Parse OpenAI-compatible rate limit headers (NVIDIA, Cerebras, Groq, Local)."""
        results = []

        # Requests remaining
        req_remaining = headers.get("x-ratelimit-remaining-requests")
        req_limit = headers.get("x-ratelimit-limit-requests")
        req_reset = headers.get("x-ratelimit-reset-requests") or headers.get(
            "x-ratelimit-reset"
        )

        if req_remaining is not None:
            results.append(
                RateLimitData(
                    limit_type="requests",
                    limit_value=int(req_limit) if req_limit else None,
                    remaining=int(req_remaining),
                    reset_timestamp=int(req_reset) if req_reset else None,
                    header_source="x-ratelimit-remaining-requests",
                )
            )

        # Tokens remaining
        tok_remaining = headers.get("x-ratelimit-remaining-tokens")
        tok_limit = headers.get("x-ratelimit-limit-tokens")
        tok_reset = headers.get("x-ratelimit-reset-tokens") or headers.get(
            "x-ratelimit-reset"
        )

        if tok_remaining is not None:
            results.append(
                RateLimitData(
                    limit_type="tokens",
                    limit_value=int(tok_limit) if tok_limit else None,
                    remaining=int(tok_remaining),
                    reset_timestamp=int(tok_reset) if tok_reset else None,
                    header_source="x-ratelimit-remaining-tokens",
                )
            )

        return results

    @staticmethod
    def parse_huggingface(headers: dict[str, str]) -> list[RateLimitData]:
        """Parse HuggingFace rate limit headers."""
        results = []

        remaining = headers.get("x-ratelimit-remaining")
        limit = headers.get("x-ratelimit-limit")
        reset = headers.get("x-ratelimit-reset")

        if remaining is not None:
            results.append(
                RateLimitData(
                    limit_type="requests",
                    limit_value=int(limit) if limit else None,
                    remaining=int(remaining),
                    reset_timestamp=int(reset) if reset else None,
                    header_source="x-ratelimit-remaining",
                )
            )

        return results

    @staticmethod
    def parse_google_ai(headers: dict[str, str]) -> list[RateLimitData]:
        """Parse Google AI rate limit headers."""
        results = []

        remaining = headers.get("x-ratelimit-remaining")
        limit = headers.get("x-ratelimit-limit")
        reset = headers.get("x-ratelimit-reset")

        if remaining is not None:
            results.append(
                RateLimitData(
                    limit_type="requests",
                    limit_value=int(limit) if limit else None,
                    remaining=int(remaining),
                    reset_timestamp=int(reset) if reset else None,
                    header_source="x-ratelimit-remaining",
                )
            )

        return results

    @staticmethod
    def parse_generic(headers: dict[str, str]) -> list[RateLimitData]:
        """Try to parse any recognizable rate limit headers."""
        results = []

        # Standard headers
        for prefix in ["x-ratelimit", "ratelimit"]:
            remaining_key = f"{prefix}-remaining"
            limit_key = f"{prefix}-limit"
            reset_key = f"{prefix}-reset"

            if remaining_key in headers:
                results.append(
                    RateLimitData(
                        limit_type="requests",
                        limit_value=int(headers[limit_key])
                        if limit_key in headers
                        else None,
                        remaining=int(headers[remaining_key]),
                        reset_timestamp=int(headers[reset_key])
                        if reset_key in headers
                        else None,
                        header_source=remaining_key,
                    )
                )

        return results

    @classmethod
    def parse_for_provider(
        cls, provider: str, headers: dict[str, str]
    ) -> list[RateLimitData]:
        """Parse rate limit headers for a specific provider."""
        parser_map = {
            "huggingface": cls.parse_huggingface,
            "google_ai": cls.parse_google_ai,
            "nvidia": cls.parse_openai_compatible,
            "cerebras": cls.parse_openai_compatible,
            "groq": cls.parse_openai_compatible,
            "local": cls.parse_openai_compatible,
        }

        parser = parser_map.get(provider, cls.parse_generic)
        return parser(headers)


def extract_usage_from_response(data: dict[str, Any]) -> tuple[int, int]:
    """Extract prompt and completion tokens from provider response."""
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    return prompt_tokens, completion_tokens


def extract_usage_from_streaming_chunk(line: str) -> tuple[int, int] | None:
    """Extract token usage from a streaming SSE line."""
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