# tests/test_routing_score.py
from __future__ import annotations

from dataclasses import replace

from llm_router.provider_matrix import get_provider_matrix_entry, load_provider_matrix
from llm_router.routing_score import ZeroCostPolicy, runtime_route_score, static_zero_cost_score


class Status:
    available = True
    last_polled = 1.0
    latency_ms = 100.0
    latency_p50_ms = 100.0
    daily_calls_used = 0
    daily_calls_remaining = 100
    daily_tokens_used = 0
    daily_tokens_remaining = 1000
    rate_limit_remaining = 10
    rate_limit_reset = None


def test_packaged_matrix_has_expected_size() -> None:
    assert len(load_provider_matrix()) == 55


def test_static_scores_match_packaged_catalog() -> None:
    mismatches = [
        (entry.id, entry.zero_cost_routing_score, static_zero_cost_score(entry))
        for entry in load_provider_matrix()
        if entry.zero_cost_routing_score != static_zero_cost_score(entry)
    ]
    assert mismatches == []


def test_groq_is_recurring_and_high_score() -> None:
    groq = get_provider_matrix_entry("groq")
    assert groq is not None
    assert groq.free_class == "recurring"
    assert groq.zero_cost_routing_score == 94


def test_new_recurring_provider_entries_are_loadable() -> None:
    z_ai = get_provider_matrix_entry("z_ai")
    llm7 = get_provider_matrix_entry("llm7")
    aion = get_provider_matrix_entry("aionlabs")

    assert z_ai is not None
    assert z_ai.id == "z-ai"
    assert z_ai.free_class == "recurring"
    assert z_ai.tokens_per_day_equivalent is None
    assert z_ai.tool_calling is True
    assert z_ai.vision is True

    assert llm7 is not None
    assert llm7.tokens_per_day_equivalent == 1_000_000
    assert llm7.tool_calling is False
    assert "may be used" in llm7.privacy.lower()

    assert aion is not None
    assert aion.tokens_per_day_equivalent == 20_000
    assert aion.tool_calling is True
    assert aion.openai_compatible == "yes"


def test_llm7_data_improvement_route_is_private_by_default() -> None:
    llm7 = get_provider_matrix_entry("llm7")
    assert llm7 is not None

    blocked = runtime_route_score("llm7", llm7, Status(), ZeroCostPolicy())
    allowed = runtime_route_score(
        "llm7",
        llm7,
        Status(),
        replace(ZeroCostPolicy(), allow_data_improvement=True),
    )

    assert blocked.eligible is False
    assert "privacy-policy" in blocked.reasons
    assert allowed.eligible is True


def test_trials_are_excluded_by_default() -> None:
    cerebras = get_provider_matrix_entry("cerebras")
    assert cerebras is not None
    score = runtime_route_score("cerebras", cerebras, Status(), ZeroCostPolicy())
    assert not score.eligible
    assert "trial-disabled" in score.reasons


def test_quota_exhaustion_blocks_route() -> None:
    groq = get_provider_matrix_entry("groq")
    assert groq is not None
    exhausted = Status()
    exhausted.daily_calls_remaining = 0
    score = runtime_route_score("groq", groq, exhausted, ZeroCostPolicy())
    assert not score.eligible
    assert "quota-exhausted" in score.reasons


def test_policy_can_enable_trials() -> None:
    cerebras = get_provider_matrix_entry("cerebras")
    assert cerebras is not None
    policy = replace(ZeroCostPolicy(), include_trials=True)
    score = runtime_route_score("cerebras", cerebras, Status(), policy)
    assert score.eligible
