# Operational Correctness Remediation

Status for the stacked operational-correctness PR.

## Implemented in this PR

- [x] Remove recurring `/models` cloud polling from daemon background tasks.
- [x] Make provider/status endpoints use cached passive telemetry.
- [x] Cache explicit model discovery for six hours with exponential failure backoff.
- [x] Move runtime SQLite and report-file work off the asyncio event loop.
- [x] Serialize SQLite operations through a dedicated worker and enable WAL/busy timeout.
- [x] Store request and token rate-limit windows independently.
- [x] Parse duration-style and malformed rate-limit reset headers safely.
- [x] Honor configured UTC quota reset hours.
- [x] Reserve quota atomically before dispatch and reconcile actual usage afterward.
- [x] Record failed/retryable attempts as well as successful requests.
- [x] Calculate p50/p99 from recorded latency samples.
- [x] Correct Gemini system instructions and assistant/model role translation.
- [x] Normalize Gemini streaming data to OpenAI SSE and record streaming usage.
- [x] Expand request schema for tools, structured output, multimodal parts, and tool responses.
- [x] Add optional router API-key protection.
- [x] Add operational regression tests and Python 3.12 CI.

## Deliberately deferred follow-up

The provider catalog remains provider/program-centric. A later PR should normalize runtime resources as:

```text
provider -> endpoint -> model -> quota window
```

That follow-up should move context length, tool support, vision support, coding benchmarks, observed reliability, and quota state to the model/endpoint level. It should also add generic configuration-driven adapters for OpenAI-compatible providers so the 52-entry catalog does not require one Python class per provider.

## Operational invariant

Starting the daemon and reading `/healthz`, `/v1/providers`, `/v1/provider-matrix`, `/v1/metrics`, or `/metrics` must not consume cloud inference/model-list quota. Remote model discovery happens only when `/v1/models` needs an uncached/forced refresh or when a real inference request is dispatched.
