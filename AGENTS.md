# AGENTS.md

## Project Shape
- FastAPI LLM router with zero-cost-first routing.
- Main entrypoint: `src/llm_router/main.py`.
- Dashboard pages: `/dashboard` and `/logs`.

## Logging Architecture
- Standard Python `logging` is the source of truth for app/router events.
- `src/llm_router/log_events.py` installs `DBEventLogHandler`, which buffers records in `APP_EVENT_BUFFER`.
- `src/llm_router/router.py` drains that buffer into `app_events` via `AsyncMetricsStore`.
- Message bodies are captured separately in `router_message_logs` when `settings.logs.log_message_bodies` is enabled.
- Request correlation uses `request_id` and context attachment in `attach_event_context()`.

## Analytics Architecture
- Router/provider attempts live in SQLite via `MetricsDB`.
- `GET /analytics/api` returns aggregated routing reliability, failover, token, and cost data.
- Cost estimates are configurable in `config.toml` under `[analytics]` and `[analytics.pricing.*]`.
- Dashboard analytics are rendered from `/dashboard/api` and `/analytics/api`.

## Config
- `config.toml` is the source of default router, metrics, logs, and analytics settings.
- `.env.example` shows the override knobs used in local development.

## Workflow
- Prefer minimal changes that keep existing behavior intact.
- Add tests for any new DB method, API payload, or UI interaction.
- Verify with `uv run pytest -q`.
- Restart the user service with `systemctl --user restart llm-router` after config/runtime changes.
