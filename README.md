# llm-router

OpenAI-compatible LLM router that bridges **multiple cloud providers** (HuggingFace, NVIDIA NIM, Cerebras, Groq, Google AI) with **local self-hosted models** (llama.cpp) as an automatic fallback.

The default configuration now supports **zero-cost-first routing**: a packaged 52-entry provider/program matrix assigns each free route a reproducible score, filters finite trials and eligibility-gated offers by policy, blends the static score with live quota/health telemetry, and moves to the next renewable route when a provider is rate-limited or exhausted.

## Architecture

- **HuggingFace**: `https://router.huggingface.co/v1` — Serverless Inference API.
- **Groq**: `https://api.groq.com/openai/v1` — OpenAI-compatible, recurring free quotas.
- **NVIDIA NIM**: `https://integrate.api.nvidia.com/v1` — OpenAI-compatible trial route.
- **Cerebras**: `https://api.cerebras.ai/v1` — OpenAI-compatible trial route.
- **Google AI**: `https://generativelanguage.googleapis.com/v1beta` — Gemini API.
- **Local**: any OpenAI-compatible local server, default `http://127.0.0.1:8081`.

### Routing strategies

- **`zero-cost`** — default. Prefer active, recurring, directly routable free providers by matrix score. Exhausted/rate-limited routes are skipped and the next eligible free route is attempted. Finite trials are preserved unless explicitly enabled.
- **`cloud-first`** — original behavior: use the resolved cloud route and fail over on retryable provider failures.
- **`local-first`** — prefer local llama.cpp before cloud routes.
- **Explicit routing** — `"model": "nvidia:meta/llama-3.3-70b-instruct"` or `"provider": "groq"` still forces that provider and bypasses zero-cost reordering.

The static score is a **routing prior, not a model benchmark**. It rewards recurring allowances, direct/API access, OpenAI compatibility, tool calling, CLI/API usability, coding quality, explicit token quotas, no-card access, and favorable privacy characteristics. See [docs/ROUTING_SCORE.md](docs/ROUTING_SCORE.md) and [docs/PROVIDER_MATRIX.md](docs/PROVIDER_MATRIX.md).

### Zero-cost policy

The defaults intentionally conserve finite credits:

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

With the current matrix, configured recurring providers such as Groq and Hugging Face are eligible by default. Google AI unpaid-service metadata is retained but its documented data-improvement terms trigger the default privacy gate; Cerebras and NVIDIA hosted trial routes are retained but are not consumed until trials are enabled.

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /healthz` | Health check + provider list |
| `GET /v1/models` | List all available models across providers |
| `GET /v1/providers` | Poll provider availability + latency + model counts |
| `GET /v1/provider-matrix` | Configured provider metadata, eligibility reasons, static score and live dynamic score |
| `GET /v1/metrics` | Router metrics for all providers |
| `GET /v1/metrics/{provider}` | Metrics for one provider |
| `GET /dashboard` | Single-page web dashboard (no auth; localhost friendly) |
| `GET /dashboard/api?days=7&events=50` | Aggregated JSON backing the dashboard |
| `POST /v1/chat/completions` | OpenAI-compatible chat completion (streaming + non-streaming) |

## Setup

```bash
cd ~/Code/projects/llm-router
cp .env.example .env
uv sync
uv run uvicorn llm_router.main:app --host 0.0.0.0 --port 8000
```

### `.env` configuration

```bash
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxx
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxx
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxx
CEREBRAS_API_KEY=csk_xxxxxxxxxxxxxxxxxxxxxxxx
GOOGLE_AI_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LOCAL_BASE_URL=http://127.0.0.1:8081
HOST=0.0.0.0
PORT=8000
```

## Usage

### Basic zero-cost-routed chat completion

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"Hello"}]}'
```

### Force a specific provider

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"nvidia:meta/llama-3.3-70b-instruct","messages":[{"role":"user","content":"Hello"}]}'

curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local:granite-3.3-2b-instruct","messages":[{"role":"user","content":"Hello"}]}'
```

### Inspect the current free-route ranking

```bash
curl http://localhost:8000/v1/provider-matrix | jq
```

Each configured provider includes `static_score`, `dynamic_score`, `eligible`, and any exclusion reasons such as `trial-disabled`, `quota-exhausted`, `rate-limit-exhausted`, or `privacy-policy`.

### Streaming

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

### Verify API keys

```bash
uv run python tools/verify_keys.py
```

## Model aliases (`config.toml`)

| Alias | Preferred provider | Model ID |
|---|---|---|
| `qwen3-8b` | HuggingFace | Qwen/Qwen3-8B |
| `qwen3-14b` | HuggingFace | Qwen/Qwen3-14B |
| `qwen2.5-7b` | HuggingFace | Qwen/Qwen2.5-7B-Instruct |
| `llama-3.1-8b` | HuggingFace | meta-llama/Llama-3.1-8B-Instruct |
| `deepseek-r1-7b` | HuggingFace | deepseek-ai/DeepSeek-R1-Distill-Qwen-7B |
| `granite` | Local | granite-3.3-2b-instruct |
| `local` | Local | granite-3.3-2b-instruct |

Under `zero-cost`, these are preferred model routes rather than hard provider pins. Use `provider` or `provider:model` when exact-provider/model semantics are required.

## Tests

```bash
uv run pytest
```

The score tests also recompute all 52 packaged matrix scores and fail if the implementation drifts from the published matrix.

## Project structure

```text
llm-router/
├── config.toml
├── docs/
│   ├── PROVIDER_MATRIX.md
│   └── ROUTING_SCORE.md
├── src/llm_router/
│   ├── data/provider_matrix_*.json
│   ├── provider_matrix.py
│   ├── routing_score.py
│   ├── zero_cost_router.py
│   ├── router.py
│   ├── main.py
│   └── providers/
└── tests/
    ├── test_router.py
    ├── test_routing_score.py
    └── test_zero_cost_router.py
```
