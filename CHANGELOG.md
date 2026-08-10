# Changelog

## Unreleased

### Fixed

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

### Added

- Long-lived model-discovery cache with exponential failure backoff and explicit refresh.
- OpenAI-compatible tool, tool-choice, structured-output, and multimodal request fields.
- Optional `ROUTER_API_KEY` protection for `/v1/*` endpoints.
- Operational correctness regression suite.
- Python 3.12 GitHub Actions CI with pytest, compile checks, Ruff critical checks, and required Pyright type checking.
