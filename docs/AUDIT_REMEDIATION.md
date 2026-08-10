# Operational Correctness Remediation

This stacked change set addresses the correctness and efficiency issues identified during the llm-router audit.

## Priority 0 — protect free quota

- Stop polling `/models` every 60 seconds as a health check.
- Make `/v1/providers` serve cached provider state instead of forcing remote probes.
- Cache model discovery separately with a long TTL and stale-while-revalidate behavior.
- Probe inactive providers with bounded exponential backoff only when needed.

## Priority 0 — remove blocking persistence from async paths

- Move SQLite reads/writes off the asyncio event loop.
- Use a dedicated metrics-store worker and in-memory routing snapshot.
- Enable WAL/busy-timeout settings for concurrent readers/writer safety.
- Avoid synchronous report writes in background async tasks.

## Priority 0 — correct quota and rate-limit accounting

- Store request and token rate-limit windows independently.
- Respect configured reset hour/timezone and provider-reported reset timestamps.
- Add atomic quota reservations before dispatch and reconcile against actual usage.
- Track attempts separately from successful calls so 429/5xx failures are visible.

## Priority 0 — provider protocol correctness

- Correct Gemini `system` and `assistant` role conversion.
- Normalize Gemini SSE into OpenAI `chat.completion.chunk` events.
- Capture Gemini streaming usage and finish reasons.
- Harden rate-limit parsing against duration-style or malformed reset headers.

## Priority 1 — capability-aware request schema

- Add OpenAI-compatible tool calling fields.
- Support structured/multimodal message content without breaking text-only clients.
- Add `response_format`, tool responses, and related payload passthrough.
- Do not treat unsupported capabilities as routing advantages.

## Priority 1 — observability and tests

- Replace fake p50/p99 estimates with actual sampled percentiles.
- Add tests for no-quota polling, concurrent reservations, reset windows, multi-limit storage, Gemini role/stream conversion, and malformed headers.
- Add CI for Python 3.12, pytest, Ruff, and Pyright.

## Follow-up architecture

Model provider resources as `provider -> model -> endpoint/quota window`, so context length, tools, vision, coding quality, and quotas can be evaluated at the model/endpoint level rather than only at provider level.
