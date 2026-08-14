# Changelog

## Unreleased

### Fixed

- Classified upstream failures by request compatibility, authentication, billing/quota, model availability, context limits, rate limits, and provider outages; automatic routing now learns model/task incompatibilities, prefers `openrouter/free` after OpenRouter access failures, preserves upstream error details, and exposes failure classes in the live dashboard.
- Made the dashboard update continuously without page reloads by using non-overlapping live API polls, no-cache responses, in-place chart updates, and immediate catch-up when a background tab becomes visible again.
- Replaced one-failure provider blocking with a three-failure threshold, exponential automatic-routing backoff, immediate rate-limit cooldown, and success-based self-healing.
- Fixed streaming requests that aborted when a provider returned an error status mid-stream (e.g. Groq HTTP 413): the router now reads the error body safely and emits a clean SSE error instead of crashing the connection.
- Treated HTTP 413 (payload too large) as retryable so zero-cost routing fails over to the next provider whose context window fits the request.
- Sanitized Gemini function-declaration schemas so OpenAI-compatible clients' JSON-Schema keywords (`$schema`, `title`, `const`, `additionalProperties`, ...) no longer break tool calls.
- Scoped forwarded-request headers to the request lifetime so streaming and non-streaming paths reset the context correctly.
- Restored automatic-routing eligibility filtering and bare provider-ID resolution so blocked providers are skipped and explicit provider selections use their configured default model.
- Restricted cross-provider client-error failover to model-not-found responses instead of replaying non-retryable 4xx requests across the routing pool.
- Protected dashboard, logs, and analytics endpoints with `ROUTER_API_KEY` whenever API authentication is configured.
- Made full request/response body logging opt-in and preserved unknown provider model limits as `NULL` instead of treating unknown capacity as zero.
- Excluded model-discovery probes from chat reliability analytics and added retention for routing-decision events.
- Removed quota-burning 60-second provider model polling from daemon background work.
- Made provider/matrix/metrics status endpoints passive and cache-backed.
- Moved runtime SQLite/report I/O off the asyncio event loop through a serialized metrics worker.
- Added atomic quota reservations so concurrent requests cannot oversubscribe the same local free-tier guard.
- Corrected daily quota windows to honor configured UTC reset hours.
- Preserved request and token rate-limit windows independently.
- Hardened rate-limit reset parsing for epoch, relative, duration, HTTP-date, and malformed values.
- Replaced estimated latency p50/p99 with percentiles from recorded request samples.
- Corrected Gemini system instructions and assistant-to-model role conversion.
- Normalized Gemini streaming responses to OpenAI `chat.completion.chunk` SSE and recorded streaming usage.
- Recorded retryable and HTTP provider failures in metrics instead of counting only successful responses.

### Changed

- Completely redesigned the web dashboard into a responsive routing control center with persistent section navigation, grouped operational metrics, provider health cards, clearer routing/analytics workspaces, a streamlined activity view, manual/live refresh controls, and overlap-safe API refreshes.

### Added

- Web dashboard (`GET /dashboard`) with auto-refreshing summary cards, per-provider usage bars, routing matrix, model list, request-kind breakdown, traffic charts, and recent routing events, plus an aggregated `GET /dashboard/api` endpoint (Chart.js vendored locally for offline use).
- Request/response classification in metrics: chat vs tool-call requests and whether each completion returned tool calls, recorded per provider event.
- Per-request routing decision log (`router_request_events`): served provider, model, stream/explicit flags, failover index, request/response kind, tokens, latency and status.
- Long-lived model-discovery cache with exponential failure backoff and explicit refresh.
- OpenAI-compatible tool, tool-choice, structured-output, and multimodal request fields.
- Optional `ROUTER_API_KEY` protection for `/v1/*` endpoints.
- Operational correctness regression suite.
- Python 3.12 GitHub Actions CI with pytest, compile checks, Ruff critical checks, and required Pyright type checking.
- Streaming failover coverage: an HTTP 413 from the primary provider now falls through to the next zero-cost route.
- ASGI streaming regression coverage for router-owned header/context lifecycle.
- Dashboard/classification regression tests.
