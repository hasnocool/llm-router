# llm-router

OpenAI-compatible LLM router that bridges multiple cloud inference providers with local self-hosted models, with quota-aware **zero-cost-first routing** and automatic fallback.

The router packages a 52-entry provider/program matrix, scores renewable free resources, observes real provider rate-limit headers, and avoids consuming finite trial credit unless policy explicitly allows it.

## Key behavior

- **Zero-cost-first routing**: recurring free routes are preferred before local compute and finite trials.
- **Passive-first health**: normal daemon operation does **not** poll cloud `/models` endpoints every minute. Real inference traffic updates health, latency, quota, and rate-limit state.
- **Cached model discovery**: `/v1/models` caches provider model lists for six hours by default and exponentially backs off failed refreshes. `?refresh=true` forces an explicit refresh.
- **Non-blocking metrics**: SQLite work is serialized on a dedicated worker thread so request handlers do not block the asyncio event loop.
- **Concurrency-safe quota guards**: optional local caps reserve request/token budget before dispatch and reconcile actual usage afterward.
- **Multi-window rate limits**: request and token limits are retained independently instead of overwriting one another.
- **Real latency percentiles**: p50/p99 are calculated from request samples rather than estimated from averages.
- **Gemini normalization**: system instructions, assistant/model roles, tool calls, and streaming responses are translated to/from the OpenAI chat-completions shape.
- **Tools and structured requests**: the public request schema supports tool definitions, tool choice, multimodal content parts, `response_format`, and related OpenAI-compatible fields.
- **Optional router authentication**: set `ROUTER_API_KEY` to protect all `/v1/*` endpoints when exposing the service beyond loopback.

## Providers

The default config includes Hugging Face, Groq, NVIDIA NIM, Cerebras, Google AI/Gemini, and a local OpenAI-compatible llama.cpp server. The provider matrix contains additional free/trial/conditional programs for future adapters and routing decisions.

## Routing strategies

- `zero-cost` — default; route among eligible renewable free resources using the provider matrix plus live telemetry.
- `cloud-first` — preserve the original primary-cloud/fallback behavior.
- `local-first` — prefer local llama.cpp first.
- Explicit `provider` or `provider:model` routing bypasses automatic reordering.

The static score is a routing prior, not a model benchmark. See [docs/ROUTING_SCORE.md](docs/ROUTING_SCORE.md), [docs/PROVIDER_MATRIX.md](docs/PROVIDER_MATRIX.md), and [docs/AUDIT_REMEDIATION.md](docs/AUDIT_REMEDIATION.md).

## Zero-cost policy

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

Provider-wide quotas are deliberately **not guessed in the default config** because free limits are often model-, tier-, and account-specific. Provider response headers are observed automatically. Optional local safety caps can be configured in `config.toml` when the exact account/model limits are known.

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /healthz` | Local router health; no cloud calls |
| `GET /v1/models` | Cached model discovery; `?refresh=true` forces a refresh |
| `GET /v1/providers` | Cached provider health/quota/latency state; no cloud calls |
| `GET /v1/provider-matrix` | Static + dynamic zero-cost scores and eligibility reasons |
| `GET /v1/metrics` | Metrics for all providers |
| `GET /v1/metrics/{provider}` | Metrics and all observed rate-limit windows for one provider |
| `GET /metrics` | Prometheus text metrics |
| `POST /v1/chat/completions` | OpenAI-compatible streaming/non-streaming inference |

## Setup

```bash
cd ~/Code/projects/llm-router
cp .env.example .env
uv sync
uv run uvicorn llm_router.main:app --host 0.0.0.0 --port 8000
```

For a LAN-accessible deployment, set a strong router key:

```bash
ROUTER_API_KEY='replace-with-a-long-random-secret'
```

Clients can then use either `Authorization: Bearer <key>` or `X-API-Key: <key>`.

## Usage

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer YOUR_ROUTER_KEY' \
  -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"Hello"}]}'
```

Inspect the current route order without triggering provider probes:

```bash
curl http://localhost:8000/v1/provider-matrix | jq
```

Refresh model discovery explicitly:

```bash
curl 'http://localhost:8000/v1/models?refresh=true' | jq
```

## Operational model

```text
real inference response
        │
        ├── provider health/latency
        ├── request + token rate-limit windows
        ├── actual token usage
        └── quota reconciliation
                 │
                 ▼
          cached route state
             ┌───┴───┐
             ▼       ▼
           router  dashboard
```

No background task calls provider model-list endpoints. The only recurring background work is local metrics retention/report maintenance, executed outside the asyncio event loop.

## Tests

```bash
uv run pytest
```

The operational suite covers quota reservations, reset-hour windows, separate request/token limits, true latency percentiles, model-cache behavior, malformed rate-limit headers, and Gemini request/stream translation. GitHub Actions runs tests on Python 3.12 plus critical Ruff checks and required Pyright type checking.

## Project structure

```text
llm-router/
├── config.toml
├── docs/
│   ├── AUDIT_REMEDIATION.md
│   ├── PROVIDER_MATRIX.md
│   └── ROUTING_SCORE.md
├── src/llm_router/
│   ├── async_metrics.py
│   ├── metrics_db.py
│   ├── metrics_report.py
│   ├── provider_matrix.py
│   ├── rate_limits.py
│   ├── router.py
│   ├── routing_score.py
│   ├── zero_cost_router.py
│   └── providers/
│       ├── base.py
│       └── google_ai.py
└── tests/
    ├── test_operational_correctness.py
    ├── test_router.py
    ├── test_routing_score.py
    └── test_zero_cost_router.py
```
