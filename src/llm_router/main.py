from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import ConfigError, load_settings
from .providers.base import ProviderRequestError, ProviderUnavailable
from .router import ModelRouter
from .schemas import ChatRequest, ModelList, ProviderInfo, ProviderList

_settings = None
_router: ModelRouter | None = None


def get_router() -> ModelRouter:
    if _router is None:
        raise RuntimeError("router not initialized")
    return _router


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _router
    try:
        _settings = load_settings()
    except ConfigError as exc:
        raise RuntimeError(f"configuration error: {exc}") from exc
    async with httpx.AsyncClient(timeout=_settings.timeout_seconds) as http:
        _router = ModelRouter(_settings, http)
        yield


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
            )
            for s in router.ranked_providers()
        ]
    )


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
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

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    resp = await router.complete(req)
    return JSONResponse(content=resp.model_dump(exclude_none=True))


def _sse_error(message: str, status: int | None = None) -> str:
    payload = {
        "error": {"message": message, "type": "router_error", "status": status},
        "provider": None,
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n"
