# src/llm_router/main.py
from __future__ import annotations

import hmac
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
from .schemas import AllMetricsResponse, ChatRequest, MetricsResponse, ModelList, ProviderInfo, ProviderList
from .zero_cost_router import ZeroCostModelRouter

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

    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)
    try:
        _settings = load_settings()
    except ConfigError as exc:
        raise RuntimeError(f"configuration error: {exc}") from exc

    limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
    async with httpx.AsyncClient(timeout=_settings.timeout_seconds, limits=limits) as http:
        router_cls = ZeroCostModelRouter if _settings.strategy == "zero-cost" else ModelRouter
        _router = router_cls(_settings, http)
        await _router.start_background_tasks()
        logger.info("llm-router started with strategy=%s", _settings.strategy)
        try:
            yield
        finally:
            await _router.stop_background_tasks()
            logger.info("llm-router stopped")


app = FastAPI(title="llm-router", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def optional_router_auth(request: Request, call_next):
    if _settings is not None and _settings.router_api_key and request.url.path.startswith("/v1/"):
        bearer = request.headers.get("authorization", "")
        supplied = bearer.removeprefix("Bearer ").strip() or request.headers.get("x-api-key", "")
        if not supplied or not hmac.compare_digest(supplied, _settings.router_api_key):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "invalid router API key", "type": "authentication_error"}},
            )
    return await call_next(request)


@app.exception_handler(ProviderRequestError)
async def on_request_error(_: Request, exc: ProviderRequestError):
    status = exc.status_code if exc.status_code and 400 <= exc.status_code < 500 else 400
    return JSONResponse(status_code=status, content={"error": {"message": str(exc), "type": "invalid_request_error"}})


@app.exception_handler(ProviderUnavailable)
async def on_unavailable(_: Request, exc: ProviderUnavailable):
    return JSONResponse(status_code=503, content={"error": {"message": str(exc), "type": "unavailable"}})


@app.exception_handler(QuotaExceededError)
async def on_quota_exceeded(_: Request, exc: QuotaExceededError):
    return JSONResponse(
        status_code=429,
        content={"error": {"message": str(exc), "type": "quota_exceeded", "provider": exc.provider}},
    )


@app.exception_handler(ConfigError)
async def on_config_error(_: Request, exc: ConfigError):
    return JSONResponse(status_code=500, content={"error": {"message": str(exc), "type": "config_error"}})


@app.get("/healthz")
async def healthz():
    router = get_router()
    return {"status": "ok", "strategy": router.settings.strategy, "providers": list(router.providers)}


@app.get("/v1/models", response_model=ModelList)
async def list_models(refresh: bool = Query(False)):
    return ModelList(data=await get_router().list_models(force_refresh=refresh))


@app.get("/v1/providers", response_model=ProviderList)
async def list_providers():
    router = get_router()
    await router.refresh_status_from_metrics()
    return ProviderList(
        providers=[
            ProviderInfo(
                name=status.name,
                available=status.available,
                model_count=status.model_count,
                latency_ms=status.latency_ms,
                last_error=status.last_error,
                last_polled=status.last_polled,
                daily_calls_used=status.daily_calls_used,
                daily_calls_remaining=status.daily_calls_remaining,
                daily_tokens_used=status.daily_tokens_used,
                daily_tokens_remaining=status.daily_tokens_remaining,
                rate_limit_remaining=status.rate_limit_remaining,
                rate_limit_reset=status.rate_limit_reset,
                latency_p50_ms=status.latency_p50_ms,
                latency_p99_ms=status.latency_p99_ms,
            )
            for status in router.ranked_providers()
        ]
    )


@app.get("/v1/provider-matrix")
async def provider_matrix():
    router = get_router()
    await router.refresh_status_from_metrics()
    if isinstance(router, ZeroCostModelRouter):
        return {
            "strategy": router.settings.strategy,
            "policy": {
                "min_score": router.zero_cost_policy.min_score,
                "include_trials": router.zero_cost_policy.include_trials,
                "include_conditional": router.zero_cost_policy.include_conditional,
                "allow_indirect": router.zero_cost_policy.allow_indirect,
                "allow_self_host": router.zero_cost_policy.allow_self_host,
                "allow_data_improvement": router.zero_cost_policy.allow_data_improvement,
                "require_openai_compatible": router.zero_cost_policy.require_openai_compatible,
            },
            "providers": router.provider_matrix_view(),
        }
    return {"strategy": router.settings.strategy, "providers": [], "message": "Set strategy = 'zero-cost' to enable matrix-aware ranking."}


@app.get("/v1/metrics", response_model=AllMetricsResponse)
async def get_metrics(days: int = Query(7, ge=1, le=90)):
    return AllMetricsResponse(providers=await get_router().get_all_metrics(days))


@app.get("/v1/metrics/{provider}", response_model=MetricsResponse)
async def get_provider_metrics(provider: str, days: int = Query(7, ge=1, le=90)):
    router = get_router()
    if provider not in router.providers:
        return JSONResponse(status_code=404, content={"error": {"message": f"provider {provider} not found", "type": "not_found"}})
    return MetricsResponse(**await router.get_provider_metrics(provider, days))


@app.get("/metrics")
async def prometheus_metrics():
    router = get_router()
    await router.refresh_status_from_metrics()
    lines: list[str] = []
    for name, status in router.status.items():
        lines.append(f'llm_router_provider_available{{provider="{name}"}} {1 if status.available else 0}')
        lines.append(f'llm_router_provider_model_count{{provider="{name}"}} {status.model_count}')
        lines.append(f'llm_router_provider_latency_ms{{provider="{name}"}} {status.latency_ms}')
        lines.append(f'llm_router_provider_calls_total{{provider="{name}"}} {status.daily_calls_used}')
        lines.append(f'llm_router_provider_tokens_total{{provider="{name}"}} {status.daily_tokens_used}')
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
        response = await router.complete(req)
        return JSONResponse(content=response.model_dump(exclude_none=True))
    finally:
        reset_forwarded_request_headers(token)


def _sse_error(message: str, status: int | None = None) -> str:
    payload = {"error": {"message": message, "type": "router_error", "status": status}, "provider": None}
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n"
