# llm-router

OpenAI-compatible LLM router that bridges **multiple cloud providers** (HuggingFace, NVIDIA NIM, Cerebras, Google AI) with **local self-hosted models** (llama.cpp) as an automatic fallback.

## Architecture

- **HuggingFace**: `https://router.huggingface.co/v1` — Serverless Inference API (Bearer token from `HF_TOKEN`).
- **NVIDIA NIM**: `https://integrate.api.nvidia.com/v1` — OpenAI-compatible (Bearer token from `NVIDIA_API_KEY`).
- **Cerebras**: `https://api.cerebras.ai/v1` — OpenAI-compatible (Bearer token from `CEREBRAS_API_KEY`).
- **Google AI**: `https://generativelanguage.googleapis.com/v1beta` — Gemini API (header `x-goog-api-key` from `GOOGLE_AI_API_KEY`).
- **Local**: any OpenAI-compatible local server, default `http://127.0.0.1:8081` (llama.cpp `llama-server`, e.g. `granite-3.3-2b-instruct`).

### Routing Strategy

- **Cloud-first** with transparent failover to local on connection errors, timeouts, HTTP 429/5xx.
- **Availability ranking**: providers are ranked by model count (most available first).
- **Explicit routing**: `"model": "nvidia:meta/llama-3.3-70b-instruct"` forces NVIDIA.
- **Local-first mode**: `"local_first": true` or `"strategy": "local-first"` in config.

### Endpoints

| Endpoint | Description |
|---|---|
| `GET /healthz` | Health check + provider list |
| `GET /v1/models` | List all available models across providers |
| `GET /v1/providers` | Poll provider availability + latency + model counts |
| `POST /v1/chat/completions` | OpenAI-compatible chat completion (streaming + non-streaming) |

## Setup

```bash
cd ~/Code/projects/llm-router
cp .env.example .env   # set your API keys
uv sync
uv run uvicorn llm_router.main:app --host 0.0.0.0 --port 8000
```

### .env Configuration

```bash
# HuggingFace (required for cloud)
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxx

# NVIDIA NIM (optional, 102+ models)
NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxx

# Cerebras (optional, check cloud.cerebras.ai)
CEREBRAS_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxx

# Google AI / Gemini (optional, check aistudio.google.com/apikey)
GOOGLE_AI_API_KEY=AIzaSyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Local llama-server
LOCAL_BASE_URL=http://127.0.0.1:8081

# Server binding
HOST=0.0.0.0
PORT=8000
```

## Usage

### Basic chat completion
```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"Hello"}]}'
```

### Force a specific provider
```bash
# Use NVIDIA
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"nvidia:meta/llama-3.3-70b-instruct","messages":[{"role":"user","content":"Hello"}]}'

# Use local model
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"local:granite-3.3-2b-instruct","messages":[{"role":"user","content":"Hello"}]}'
```

### Check provider availability
```bash
curl http://localhost:8000/v1/providers | jq
```

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

## Model Aliases (config.toml)

| Alias | Provider | Model ID |
|---|---|---|
| `qwen3-8b` | HuggingFace | Qwen/Qwen3-8B |
| `qwen3-14b` | HuggingFace | Qwen/Qwen3-14B |
| `qwen2.5-7b` | HuggingFace | Qwen/Qwen2.5-7B-Instruct |
| `llama-3.1-8b` | HuggingFace | meta-llama/Llama-3.1-8B-Instruct |
| `deepseek-r1-7b` | HuggingFace | deepseek-ai/DeepSeek-R1-Distill-Qwen-7B |
| `granite` | Local | granite-3.3-2b-instruct |
| `local` | Local | granite-3.3-2b-instruct |

## Tests

```bash
uv run pytest
```

## Project Structure

```
llm-router/
├── pyproject.toml
├── config.toml              # Provider definitions + model aliases
├── .env                     # API keys (gitignored)
├── .env.example             # Template
├── src/llm_router/
│   ├── main.py              # FastAPI app (3 endpoints + error handlers)
│   ├── config.py            # Config loader (.env + config.toml)
│   ├── schemas.py           # OpenAI-compatible Pydantic models
│   ├── router.py            # Cloud-first failover + availability polling
│   └── providers/
│       ├── base.py          # Provider ABC (complete, stream, models)
│       ├── huggingface.py   # HuggingFace Serverless Inference
│       ├── nvidia.py        # (uses base.py — OpenAI-compatible)
│       ├── cerebras.py      # (uses base.py — OpenAI-compatible)
│       ├── google_ai.py     # Google AI Gemini API (custom format)
│       └── local.py         # Local llama-server
├── tests/
│   └── test_router.py       # Unit tests (13 tests)
└── tools/
    └── verify_keys.py       # API key verification script
```
