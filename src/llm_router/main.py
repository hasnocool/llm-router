from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .config import ConfigError, load_settings
from .providers.base import (
    ProviderRequestError,
    ProviderUnavailable,
    QuotaExceededError,
    reset_forwarded_request_headers,
    set_forwarded_request_headers,
)
from .router import ModelRouter
from .schemas import (
    AllMetricsResponse,
    ChatRequest,
    MetricsResponse,
    ModelList,
    ProviderInfo,
    ProviderList,
)

_settings = None
_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    if _router is None:
        raise RuntimeError("router not initialized")
    return _router


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _router
    import logging
    logging.basicConfig(level=logging.DEBUG)
    logger = logging.getLogger(__name__)
    try:
        _settings = load_settings()
    except ConfigError as exc:
        raise RuntimeError(f"configuration error: {exc}") from exc
    async with httpx.AsyncClient(timeout=_settings.timeout_seconds) as http:
        _router = ModelRouter(_settings, http)
        _router.start_background_tasks()
        logger.info("Background tasks started, yielding...")
        try:
            yield
        finally:
            logger.info("Shutting down background tasks...")
            await _router.stop_background_tasks()
            logger.info("Background tasks stopped")


app = FastAPI(title="llm-router", version="0.1.0", lifespan=lifespan)


@app.exception_handler(ProviderRequestError)
async def on_request_error(_: Request, exc: ProviderRequestError):
    status = exc.status_code if exc.status_code and 400 <= exc.status_code < 500 else 400
    return JSONResponse(
        status_code=status,
        content={"error": {"message": str(exc), "type": "invalid_request_error"}},
    )


@app.exception_handler(ProviderUnavailable)
async def on_unavailable(_: Request, exc: ProviderUnavailable):
    return JSONResponse(
        status_code=503,
        content={"error": {"message": str(exc), "type": "unavailable"}},
    )


@app.exception_handler(QuotaExceededError)
async def on_quota_exceeded(_: Request, exc: QuotaExceededError):
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "message": str(exc),
                "type": "quota_exceeded",
                "provider": exc.provider,
            }
        },
    )


@app.exception_handler(ConfigError)
async def on_config_error(_: Request, exc: ConfigError):
    return JSONResponse(
        status_code=500,
        content={"error": {"message": str(exc), "type": "config_error"}},
    )


@app.get("/healthz")
async def healthz():
    router = get_router()
    provider_names = list(router.providers)
    return {"status": "ok", "strategy": router.settings.strategy, "providers": provider_names}


@app.get("/v1/models", response_model=ModelList)
async def list_models():
    return ModelList(data=await get_router().list_models())


@app.get("/v1/providers", response_model=ProviderList)
async def list_providers():
    router = get_router()
    status = await router.poll_all_providers()
    return ProviderList(
        providers=[
            ProviderInfo(
                name=s.name,
                available=s.available,
                model_count=s.model_count,
                latency_ms=s.latency_ms,
                last_error=s.last_error,
                last_polled=s.last_polled,
                daily_calls_used=s.daily_calls_used,
                daily_calls_remaining=s.daily_calls_remaining,
                daily_tokens_used=s.daily_tokens_used,
                daily_tokens_remaining=s.daily_tokens_remaining,
                rate_limit_remaining=s.rate_limit_remaining,
                rate_limit_reset=s.rate_limit_reset,
                latency_p50_ms=s.latency_p50_ms,
                latency_p99_ms=s.latency_p99_ms,
            )
            for s in router.ranked_providers()
        ]
    )


@app.get("/v1/metrics", response_model=AllMetricsResponse)
async def get_metrics(days: int = Query(7, ge=1, le=90)):
    router = get_router()
    return AllMetricsResponse(providers=router.get_all_metrics(days))


@app.get("/v1/metrics/{provider}", response_model=MetricsResponse)
async def get_provider_metrics(provider: str, days: int = Query(7, ge=1, le=90)):
    router = get_router()
    if provider not in router.providers:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": f"provider {provider} not found", "type": "not_found"}},
        )
    return MetricsResponse(**router.get_provider_metrics(provider, days))


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus metrics endpoint."""
    router = get_router()
    lines = []

    # Provider info
    for name, provider in router.providers.items():
        status = router.status.get(name)
        if not status:
            continue

        lines.append(f'llm_router_provider_available{{provider="{name}"}} {1 if status.available else 0}')
        lines.append(f'llm_router_provider_model_count{{provider="{name}"}} {status.model_count}')
        lines.append(f'llm_router_provider_latency_ms{{provider="{name}"}} {status.latency_ms}')
        lines.append(f'llm_router_provider_calls_total{{provider="{name}"}} {status.daily_calls_used}')
        lines.append(f'llm_router_provider_tokens_total{{provider="{name}",type="prompt"}} {status.daily_tokens_used}')

        if status.daily_calls_remaining is not None:
            lines.append(f'llm_router_provider_calls_remaining{{provider="{name}"}} {status.daily_calls_remaining}')
        if status.daily_tokens_remaining is not None:
            lines.append(f'llm_router_provider_tokens_remaining{{provider="{name}"}} {status.daily_tokens_remaining}')
        if status.rate_limit_remaining is not None:
            lines.append(f'llm_router_provider_rate_limit_remaining{{provider="{name}"}} {status.rate_limit_remaining}')
        if status.rate_limit_reset is not None:
            lines.append(f'llm_router_provider_rate_limit_reset{{provider="{name}"}} {status.rate_limit_reset}')
        lines.append(f'llm_router_provider_latency_p50_ms{{provider="{name}"}} {status.latency_p50_ms}')
        lines.append(f'llm_router_provider_latency_p99_ms{{provider="{name}"}} {status.latency_p99_ms}')

    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, request: Request):
    token = set_forwarded_request_headers(dict(request.headers))
    router = get_router()
    if req.stream:
        async def event_source():
            try:
                async for line in router.stream(req):
                    yield line + "\n"
            except ProviderUnavailable as exc:
                yield _sse_error(str(exc))
            except ProviderRequestError as exc:
                yield _sse_error(str(exc), status=exc.status_code)
            finally:
                reset_forwarded_request_headers(token)

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        resp = await router.complete(req)
        return JSONResponse(content=resp.model_dump(exclude_none=True))
    finally:
        reset_forwarded_request_headers(token)


def _sse_error(message: str, status: int | None = None) -> str:
    payload = {
        "error": {"message": message, "type": "router_error", "status": status},
        "provider": None,
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n"