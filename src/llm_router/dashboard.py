# src/llm_router/dashboard.py
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

STATIC_DIR = Path(__file__).resolve().parent / "static"
DASHBOARD_HTML = STATIC_DIR / "dashboard.html"
LOGS_HTML = STATIC_DIR / "logs.html"

router = APIRouter()


@router.get("/dashboard")
async def dashboard_page() -> FileResponse:
    if not DASHBOARD_HTML.exists():
        raise HTTPException(status_code=404, detail="dashboard page not built")
    return FileResponse(DASHBOARD_HTML)


@router.get("/logs")
async def logs_page() -> FileResponse:
    if not LOGS_HTML.exists():
        raise HTTPException(status_code=404, detail="logs page not built")
    return FileResponse(LOGS_HTML)


@router.get("/dashboard/api")
async def dashboard_api(
    days: int = Query(7, ge=1, le=90),
    events: int = Query(50, ge=1, le=500),
) -> JSONResponse:
    from .main import get_router
    data = await get_router().get_dashboard_data(days=days, events=events)
    return JSONResponse(content=data)


@router.get("/analytics/api")
async def analytics_api(
    days: int = Query(30, ge=1, le=365),
) -> JSONResponse:
    from .main import get_router
    data = await get_router().get_analytics_data(days=days)
    return JSONResponse(content=data)


@router.get("/logs/api")
async def logs_api(
    days: int = Query(1, ge=1, le=90),
    limit: int = Query(200, ge=1, le=1000),
    level: str = Query("all"),
    provider: str = Query(""),
    request_id: str = Query(""),
    view: str = Query("both"),
) -> JSONResponse:
    from .main import get_router
    data = await get_router().get_logs_data(
        days=days,
        limit=limit,
        level=level or None,
        provider=provider or None,
        request_id=request_id or None,
        messages_only=view == "messages",
        events_only=view == "events",
    )
    return JSONResponse(content=data)
