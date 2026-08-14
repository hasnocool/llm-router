# src/llm_router/routing_score.py
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from .provider_matrix import ProviderMatrixEntry


class ProviderStatusLike(Protocol):
    available: bool
    last_polled: float
    latency_ms: float
    latency_p50_ms: float
    daily_calls_used: int
    daily_calls_remaining: int | None
    daily_tokens_used: int
    daily_tokens_remaining: int | None
    rate_limit_remaining: int | None
    rate_limit_reset: int | None


@dataclass(frozen=True, slots=True)
class ZeroCostPolicy:
    """Policy gates applied before dynamic route ranking."""

    min_score: int = 60
    include_trials: bool = False
    include_conditional: bool = False
    allow_indirect: bool = False
    allow_self_host: bool = False
    allow_data_improvement: bool = False
    require_openai_compatible: bool = False
    local_score: int = 76
    primary_bonus: float = 2.0

    @classmethod
    def from_env(cls) -> "ZeroCostPolicy":
        return cls(
            min_score=_env_int("LLM_ROUTER_MIN_ZERO_COST_SCORE", 60),
            include_trials=_env_bool("LLM_ROUTER_INCLUDE_TRIALS", False),
            include_conditional=_env_bool("LLM_ROUTER_INCLUDE_CONDITIONAL", False),
            allow_indirect=_env_bool("LLM_ROUTER_ALLOW_INDIRECT", False),
            allow_self_host=_env_bool("LLM_ROUTER_ALLOW_SELF_HOST", False),
            allow_data_improvement=_env_bool("LLM_ROUTER_ALLOW_DATA_IMPROVEMENT", False),
            require_openai_compatible=_env_bool("LLM_ROUTER_REQUIRE_OPENAI_COMPATIBLE", False),
            local_score=_env_int("LLM_ROUTER_LOCAL_SCORE", 76),
            primary_bonus=float(os.getenv("LLM_ROUTER_PRIMARY_BONUS", "2.0")),
        )


@dataclass(frozen=True, slots=True)
class RouteScore:
    provider: str
    matrix_id: str | None
    static_score: int
    dynamic_score: float
    free_class: str
    eligible: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    tokens_per_day_equivalent: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "matrix_id": self.matrix_id,
            "static_score": self.static_score,
            "dynamic_score": round(self.dynamic_score, 2),
            "free_class": self.free_class,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
            "tokens_per_day_equivalent": self.tokens_per_day_equivalent,
        }


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def static_zero_cost_score(provider: Mapping[str, Any] | ProviderMatrixEntry) -> int:
    """Reproduce the catalog's documented 0-100 static routing score."""
    if isinstance(provider, ProviderMatrixEntry):
        p: Mapping[str, Any] = {
            "free_class": provider.free_class,
            "router_eligible": provider.router_eligible,
            "openai_compatible": provider.openai_compatible,
            "tool_calling": provider.tool_calling,
            "cli_support": provider.cli_support,
            "coding_quality": provider.coding_quality,
            "tokens_per_day_equivalent": provider.tokens_per_day_equivalent,
            "credit_card_required": provider.credit_card_required,
            "vision": provider.vision,
            "privacy": provider.privacy,
            "status": provider.status,
            "notes": provider.notes,
        }
    else:
        p = provider

    free_class = str(p.get("free_class") or "")
    router_eligible = str(p.get("router_eligible") or "none")
    openai_compatible = str(p.get("openai_compatible") or "unknown")
    cli_support = str(p.get("cli_support") or "unknown")
    tool_calling = p.get("tool_calling")

    score = {"recurring": 30, "trial": 12, "conditional": 7}.get(free_class, 0)
    score += {"direct": 15, "self_host": 9, "indirect": 5, "none": 0}.get(
        router_eligible, 0
    )
    score += {"yes": 8, "partial": 4, "no": 0, "unknown": 1}.get(
        openai_compatible, 0
    )
    tool_scores: dict[object, int] = {
        True: 7,
        "yes": 7,
        "model-dependent": 4,
        "partial": 3,
        False: 0,
        "unknown": 1,
    }
    score += tool_scores.get(tool_calling, 0)
    score += {"native": 6, "api-compatible": 4, "indirect": 2, "none": 0, "unknown": 1}.get(
        cli_support, 0
    )
    score += round(float(p.get("coding_quality") or 0) * 4)

    tpd = p.get("tokens_per_day_equivalent")
    if isinstance(tpd, (int, float)) and tpd > 0:
        if tpd >= 5_000_000:
            score += 12
        elif tpd >= 500_000:
            score += 10
        elif tpd >= 100_000:
            score += 8
        elif tpd >= 10_000:
            score += 5
        elif tpd >= 1_000:
            score += 2

    card = p.get("credit_card_required")
    if card is False:
        score += 5
    elif card == "conditional":
        score += 2

    vision = p.get("vision")
    if vision is True or vision == "yes":
        score += 2
    elif vision == "model-dependent":
        score += 1

    privacy = str(p.get("privacy", "")).lower()
    if "zero-retention" in privacy or "zero retention" in privacy:
        score += 3
    if "may be used" in privacy or "may train" in privacy or "improve models" in privacy:
        score -= 3

    if str(p.get("status") or "") != "active":
        score -= 50
    if free_class == "trial":
        score -= 3
    notes_value = p.get("notes", ())
    notes = " ".join(map(str, notes_value if isinstance(notes_value, (list, tuple)) else (notes_value,)))
    if "limited time" in notes.lower():
        score -= 4

    return max(0, min(100, int(score)))


def privacy_disallowed(entry: ProviderMatrixEntry, allow_data_improvement: bool) -> bool:
    if allow_data_improvement:
        return False
    privacy = entry.privacy.lower()
    markers = (
        "may be used",
        "data authorization",
        "improve the model",
        "improve models",
        "improve google products",
    )
    return any(marker in privacy for marker in markers)


def matrix_policy_reasons(entry: ProviderMatrixEntry, policy: ZeroCostPolicy) -> list[str]:
    reasons: list[str] = []
    if entry.status != "active":
        reasons.append("inactive")
    if entry.zero_cost_routing_score < policy.min_score:
        reasons.append("below-min-score")
    if entry.free_class == "trial" and not policy.include_trials:
        reasons.append("trial-disabled")
    if entry.free_class == "conditional" and not policy.include_conditional:
        reasons.append("conditional-disabled")
    if entry.router_eligible == "indirect" and not policy.allow_indirect:
        reasons.append("indirect-disabled")
    if entry.router_eligible == "self_host" and not policy.allow_self_host:
        reasons.append("self-host-disabled")
    if entry.router_eligible == "none":
        reasons.append("not-router-eligible")
    if policy.require_openai_compatible and entry.openai_compatible != "yes":
        reasons.append("not-openai-compatible")
    if privacy_disallowed(entry, policy.allow_data_improvement):
        reasons.append("privacy-policy")
    return reasons


def _quota_fraction(used: int, remaining: int | None) -> float | None:
    if remaining is None:
        return None
    total = max(0, used) + max(0, remaining)
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, remaining / total))


def runtime_route_score(
    provider_name: str,
    entry: ProviderMatrixEntry | None,
    status: ProviderStatusLike | None,
    policy: ZeroCostPolicy,
    *,
    primary: bool = False,
) -> RouteScore:
    """Blend static catalog score with live health/quota telemetry."""
    if provider_name == "local" and entry is None:
        base = policy.local_score
        reasons: list[str] = []
        free_class = "local"
        matrix_id = None
        tokens = None
    elif entry is None:
        base = 40
        reasons = ["missing-matrix-entry"]
        if base < policy.min_score:
            reasons.append("below-min-score")
        free_class = "unknown"
        matrix_id = None
        tokens = None
    else:
        base = entry.zero_cost_routing_score
        reasons = matrix_policy_reasons(entry, policy)
        free_class = entry.free_class
        matrix_id = entry.id
        tokens = entry.tokens_per_day_equivalent

    score = float(base)
    if primary:
        score += policy.primary_bonus

    if status is not None:
        if status.last_polled > 0 and not status.available:
            # Tiered failures only hard-block a route during its active
            # cooldown. Once the cooldown expires, allow a probe request so a
            # recovered provider can self-heal without a process restart.
            backoff_until = float(getattr(status, "backoff_until", 0.0) or 0.0)
            if backoff_until > time.time():
                reasons.append("unavailable")

        if status.rate_limit_remaining == 0:
            reset = status.rate_limit_reset
            if reset is None or reset > int(time.time()):
                reasons.append("rate-limit-exhausted")

        call_fraction = _quota_fraction(status.daily_calls_used, status.daily_calls_remaining)
        token_fraction = _quota_fraction(status.daily_tokens_used, status.daily_tokens_remaining)
        fractions = [v for v in (call_fraction, token_fraction) if v is not None]
        if fractions:
            headroom = min(fractions)
            if headroom <= 0:
                reasons.append("quota-exhausted")
            elif headroom < 0.10:
                score -= 12.0
            elif headroom < 0.25:
                score -= 8.0
            elif headroom < 0.50:
                score -= 4.0

        latency = status.latency_ms or status.latency_p50_ms
        if latency and latency > 1000:
            score -= min(8.0, math.log2(latency / 1000.0 + 1.0) * 2.5)

    hard_blocks = {
        "inactive",
        "below-min-score",
        "trial-disabled",
        "conditional-disabled",
        "indirect-disabled",
        "self-host-disabled",
        "not-router-eligible",
        "not-openai-compatible",
        "privacy-policy",
        "unavailable",
        "rate-limit-exhausted",
        "quota-exhausted",
    }
    eligible = not any(reason in hard_blocks for reason in reasons)
    return RouteScore(
        provider=provider_name,
        matrix_id=matrix_id,
        static_score=base,
        dynamic_score=score,
        free_class=free_class,
        eligible=eligible,
        reasons=tuple(dict.fromkeys(reasons)),
        tokens_per_day_equivalent=tokens,
    )


def route_sort_key(route: RouteScore) -> tuple[int, int, float, float, str]:
    class_priority = {"recurring": 0, "local": 1, "trial": 2, "conditional": 3, "unknown": 8}
    return (
        0 if route.eligible else 1,
        class_priority.get(route.free_class, 9),
        -route.dynamic_score,
        -float(route.tokens_per_day_equivalent or 0),
        route.provider,
    )
