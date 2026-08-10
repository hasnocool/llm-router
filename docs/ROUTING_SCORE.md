# Zero-cost routing score

`zero_cost_routing_score` is a **0–100 routing heuristic**, not a model benchmark. It answers:

> How useful is this provider/program as a legitimate, automatable, zero-cost-first route for coding and agent workloads?

The exact static formula lives in `src/llm_router/routing_score.py::static_zero_cost_score`. Tests recompute every packaged matrix entry and fail if code and data drift.

## Static components

| Signal | Effect |
|---|---:|
| Recurring allowance | +30 |
| One-time trial | +12 before trial penalty |
| Conditional program | +7 |
| Direct API/gateway | +15 |
| Self-hostable free compute | +9 |
| Indirect IDE/agent access | +5 |
| OpenAI-compatible | up to +8 |
| Tool calling | up to +7 |
| Native/API-friendly CLI access | up to +6 |
| Coding quality estimate | up to +20 |
| Explicit token/day allowance | up to +12 |
| No card required | +5 |
| Vision support | up to +2 |
| Strong zero-retention wording | +3 |
| Free data may be used for improvement | -3 |
| One-time trial penalty | -3 |
| Explicit limited-time offer | -4 |

Scores are clamped to 0–100.

### Token-quota points

The token allowance bonus is deliberately based only on published token-denominated quotas:

| Published equivalent | Points |
|---:|---:|
| >= 5,000,000 tokens/day | +12 |
| >= 500,000 | +10 |
| >= 100,000 | +8 |
| >= 10,000 | +5 |
| >= 1,000 | +2 |

Request quotas, dollar credits, GPU minutes, Neurons, agent credits, or dynamic dashboard quotas are **not converted into fake tokens**.

## `coding_quality`

`coding_quality` is a coarse **0–5 prior** based on the models/features the provider exposes and whether the service is designed for coding agents. It is not an independently reproduced benchmark. Static scoring converts it to at most 20 points (`round(coding_quality * 4)`).

Production routing should treat this as an initial prior and increasingly rely on measured success rate, valid tool calls, latency, context failures, and retry rate.

## Runtime score

`runtime_route_score` starts with the static score, applies a small bonus to the caller's preferred provider/model route, and then applies live health signals already kept by `ModelRouter`:

- provider known-unavailable after polling -> blocked;
- active provider rate limit with zero remaining -> blocked until its reset;
- configured request/token quota exhausted -> blocked;
- <10% quota headroom -> -12;
- <25% quota headroom -> -8;
- <50% quota headroom -> -4;
- latency above one second -> logarithmic penalty capped at -8.

The matrix is loaded once with `importlib.resources` and cached. Scoring itself performs no network request and adds no synchronous provider/file I/O to the per-request async path.

## Default policy

`ZeroCostPolicy` defaults to:

- active entries only;
- static score >= 60;
- recurring resources only;
- direct routes only;
- exclude data-improvement/privacy-sensitive free offers;
- exclude conditional/eligibility-gated programs;
- exclude one-time trials;
- exclude catalog self-host compute;
- retain the already-configured local llama.cpp server as a fallback after renewable free APIs.

Renewable third-party free quotas sort before local compute. Local compute sorts before finite trials, so finite signup credits are preserved unless the operator enables them.

## Policy environment variables

```bash
LLM_ROUTER_MIN_ZERO_COST_SCORE=60
LLM_ROUTER_INCLUDE_TRIALS=false
LLM_ROUTER_INCLUDE_CONDITIONAL=false
LLM_ROUTER_ALLOW_INDIRECT=false
LLM_ROUTER_ALLOW_SELF_HOST=false
LLM_ROUTER_ALLOW_DATA_IMPROVEMENT=false
LLM_ROUTER_REQUIRE_OPENAI_COMPATIBLE=false
LLM_ROUTER_LOCAL_SCORE=76
LLM_ROUTER_PRIMARY_BONUS=2.0
```

## Routing behavior

Set in `config.toml`:

```toml
strategy = "zero-cost"
```

An unforced request is ordered by policy + score. Each provider after the preferred route uses that provider's configured default model, avoiding assumptions that the same model ID exists everywhere.

Explicit `provider` or `provider:model` requests still bypass zero-cost reordering.

When a zero-cost route returns a provider-side retryable error or the local quota guard raises `QuotaExceededError`, `ZeroCostModelRouter` continues to the next eligible route. The failed route is immediately demoted in in-memory status so the next request does not hammer the same exhausted provider. Provider polling can restore it once health/quota state recovers.

## Observability

`GET /v1/provider-matrix` returns configured provider metadata plus:

- `static_score`;
- `dynamic_score`;
- `eligible`;
- exclusion/demotion reasons;
- token/day equivalent when it is legitimately known.

This makes the router's choice inspectable rather than opaque.
