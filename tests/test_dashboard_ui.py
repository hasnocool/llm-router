# tests/test_dashboard_ui.py
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


STATIC = Path(__file__).parents[1] / "src" / "llm_router" / "static"


class DashboardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.nav_targets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        if tag == "a" and values.get("data-section"):
            self.nav_targets.append(str(values["data-section"]))


def _parse_dashboard() -> DashboardParser:
    parser = DashboardParser()
    parser.feed((STATIC / "dashboard.html").read_text(encoding="utf-8"))
    return parser


def test_dashboard_shell_has_unique_ids_and_valid_navigation_targets() -> None:
    parser = _parse_dashboard()
    assert len(parser.ids) == len(set(parser.ids))
    assert parser.nav_targets
    assert set(parser.nav_targets) <= set(parser.ids)


def test_dashboard_shell_keeps_render_contract_ids() -> None:
    parser = _parse_dashboard()
    required = {
        "strategy",
        "status",
        "status-dot",
        "updated",
        "days",
        "autorefresh",
        "refresh-now",
        "summary",
        "health-badge",
        "providers",
        "matrix",
        "models",
        "analytics-alerts",
        "analytics-summary",
        "analytics-providers",
        "events",
        "chart-calls",
        "chart-tokens",
        "chart-latency",
        "chart-kinds",
        "chart-cost",
        "chart-success",
        "chart-activity",
        "chart-errors",
        "chart-tasks",
    }
    assert required <= set(parser.ids)


def test_dashboard_client_uses_non_overlapping_live_refresh_and_provider_recovery_fields() -> None:
    script = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    assert "AbortController" in script
    assert "refreshInFlight && !force" in script
    assert "activeRequest.abort()" in script
    assert 'cache: "no-store"' in script
    assert 'chart.update("none")' in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert "document.hidden" in script
    assert "IntersectionObserver" in script
    assert "provider.in_backoff" in script
    assert "provider.consecutive_failures" in script
    assert "/dashboard/api?days=" in script


def test_dashboard_api_disables_http_caching() -> None:
    source = (STATIC.parent / "dashboard.py").read_text(encoding="utf-8")
    assert '"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"' in source
    assert '"Pragma": "no-cache"' in source


def test_dashboard_styles_include_responsive_and_reduced_motion_layouts() -> None:
    styles = (STATIC / "dashboard.css").read_text(encoding="utf-8")
    assert "@media (max-width: 860px)" in styles
    assert "@media (max-width: 620px)" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert ".provider-grid" in styles
    assert ".metric-grid" in styles
