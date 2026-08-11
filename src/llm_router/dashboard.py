# src/llm_router/dashboard.py
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

STATIC_DIR = Path(__file__).resolve().parent / "static"
DASHBOARD_HTML = STATIC_DIR / "dashboard.html"

router = APIRouter()


@router.get("/dashboard")
async def dashboard_page() -> FileResponse:
    if not DASHBOARD_HTML.exists():
        raise HTTPException(status_code=404, detail="dashboard page not built")
    return FileResponse(DASHBOARD_HTML)


@router.get("/dashboard/api")
async def dashboard_api(
    days: int = Query(7, ge=1, le=90),
    events: int = Query(50, ge=1, le=500),
) -> JSONResponse:
    from .main import get_router
    data = await get_router().get_dashboard_data(days=days, events=events)
    return JSONResponse(content=data)
