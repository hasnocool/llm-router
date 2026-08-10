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
    assert len(load_provider_matrix()) == 52


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
