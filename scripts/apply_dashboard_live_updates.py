from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


js = "src/llm_router/static/dashboard.js"
replace_once(
    js,
    "let activeRequest = null;\nlet toastTimer = null;",
    "let activeRequest = null;\nlet refreshInFlight = false;\nlet toastTimer = null;",
    "refresh state",
)

old_load = '''async function load({ notify = false } = {}) {
  const version = ++loadVersion;
  const days = document.getElementById("days").value;
  if (activeRequest) activeRequest.abort();
  activeRequest = new AbortController();
  setLoading(true);
  setConnection("loading", "refreshing");

  try {
    const resp = await fetch(`/dashboard/api?days=${encodeURIComponent(days)}`, {
      signal: activeRequest.signal,
      headers: { "Accept": "application/json" },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const nextState = await resp.json();
    if (version !== loadVersion) return;
    state = nextState;
    render();
    setConnection("fresh", "connected");
    document.getElementById("updated").textContent = `updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    if (notify) showToast("Dashboard refreshed");
  } catch (err) {
    if (err.name === "AbortError" || version !== loadVersion) return;
    setConnection("stale", "API unavailable");
    if (notify) showToast(`Refresh failed: ${err.message}`, "error");
  } finally {
    if (version === loadVersion) setLoading(false);
  }
}'''
new_load = '''async function load({ notify = false, force = false } = {}) {
  // Automatic refreshes never cancel one another. If the API is slower than
  // the refresh interval, let the current request finish and render it rather
  // than creating an endless abort/retry loop. User-triggered refreshes may
  // explicitly replace the in-flight request.
  if (refreshInFlight && !force) return;
  if (force && activeRequest) activeRequest.abort();

  const version = ++loadVersion;
  const days = document.getElementById("days").value;
  const controller = new AbortController();
  activeRequest = controller;
  refreshInFlight = true;
  setLoading(true);
  if (!state) setConnection("loading", "refreshing");

  try {
    const resp = await fetch(`/dashboard/api?days=${encodeURIComponent(days)}`, {
      signal: controller.signal,
      cache: "no-store",
      headers: {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
      },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const nextState = await resp.json();
    if (version !== loadVersion) return;
    state = nextState;
    render();
    setConnection("fresh", "live");
    document.getElementById("updated").textContent = `updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
    if (notify) showToast("Dashboard refreshed");
  } catch (err) {
    if (err.name === "AbortError" || version !== loadVersion) return;
    setConnection("stale", navigator.onLine ? "API unavailable" : "offline");
    if (notify) showToast(`Refresh failed: ${err.message}`, "error");
  } finally {
    if (activeRequest === controller) {
      activeRequest = null;
      refreshInFlight = false;
      if (version === loadVersion) setLoading(false);
    }
  }
}'''
replace_once(js, old_load, new_load, "load function")

old_chart = '''  if (charts[canvasId]) charts[canvasId].destroy();
  charts[canvasId] = new Chart(canvas, {
    type,
    data: { labels, datasets },
    options: chartOptions(extra),
  });'''
new_chart = '''  if (charts[canvasId]) {
    const chart = charts[canvasId];
    chart.data.labels = labels;
    chart.data.datasets = datasets;
    chart.options = chartOptions(extra);
    chart.update("none");
    return;
  }
  charts[canvasId] = new Chart(canvas, {
    type,
    data: { labels, datasets },
    options: chartOptions(extra),
  });'''
replace_once(js, old_chart, new_chart, "chart live update")

old_refresh = '''function startRefresh() {
  if (!timer) timer = setInterval(() => load(), REFRESH_MS);
}

function stopRefresh() {
  if (timer) clearInterval(timer);
  timer = null;
}'''
new_refresh = '''function startRefresh() {
  if (timer) return;
  const tick = async () => {
    timer = null;
    if (!document.hidden) await load();
    if (document.getElementById("autorefresh").checked) {
      timer = setTimeout(tick, REFRESH_MS);
    }
  };
  timer = setTimeout(tick, REFRESH_MS);
}

function stopRefresh() {
  if (timer) clearTimeout(timer);
  timer = null;
}'''
replace_once(js, old_refresh, new_refresh, "refresh scheduler")

old_handlers = '''document.getElementById("days").addEventListener("change", () => load({ notify: true }));
document.getElementById("refresh-now").addEventListener("click", () => load({ notify: true }));
document.getElementById("autorefresh").addEventListener("change", (event) => {
  if (event.target.checked) {
    startRefresh();
    load({ notify: true });
  } else {
    stopRefresh();
    showToast("Live refresh paused");
  }
});

setupNavigation();
load();
startRefresh();'''
new_handlers = '''document.getElementById("days").addEventListener("change", () => load({ notify: true, force: true }));
document.getElementById("refresh-now").addEventListener("click", () => load({ notify: true, force: true }));
document.getElementById("autorefresh").addEventListener("change", (event) => {
  if (event.target.checked) {
    startRefresh();
    load({ notify: true, force: true });
  } else {
    stopRefresh();
    showToast("Live refresh paused");
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && document.getElementById("autorefresh").checked) {
    load({ force: true });
    startRefresh();
  }
});
window.addEventListener("online", () => {
  if (document.getElementById("autorefresh").checked) load({ force: true });
});
window.addEventListener("offline", () => setConnection("stale", "offline"));

setupNavigation();
load({ force: true });
startRefresh();'''
replace_once(js, old_handlers, new_handlers, "live event handlers")

replace_once(
    "src/llm_router/dashboard.py",
    '''    data = await get_router().get_dashboard_data(days=days, events=events)\n    return JSONResponse(content=data)''',
    '''    data = await get_router().get_dashboard_data(days=days, events=events)\n    return JSONResponse(\n        content=data,\n        headers={\n            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",\n            "Pragma": "no-cache",\n            "Expires": "0",\n        },\n    )''',
    "dashboard cache headers",
)

replace_once(
    "tests/test_dashboard_ui.py",
    '''def test_dashboard_client_uses_abortable_refresh_and_provider_recovery_fields() -> None:\n    script = (STATIC / "dashboard.js").read_text(encoding="utf-8")\n    assert "AbortController" in script\n    assert "activeRequest.abort()" in script\n    assert "IntersectionObserver" in script\n    assert "provider.in_backoff" in script\n    assert "provider.consecutive_failures" in script\n    assert "/dashboard/api?days=" in script''',
    '''def test_dashboard_client_uses_non_overlapping_live_refresh_and_provider_recovery_fields() -> None:\n    script = (STATIC / "dashboard.js").read_text(encoding="utf-8")\n    assert "AbortController" in script\n    assert "refreshInFlight && !force" in script\n    assert "activeRequest.abort()" in script\n    assert 'cache: "no-store"' in script\n    assert 'chart.update("none")' in script\n    assert 'document.addEventListener("visibilitychange"' in script\n    assert "document.hidden" in script\n    assert "IntersectionObserver" in script\n    assert "provider.in_backoff" in script\n    assert "provider.consecutive_failures" in script\n    assert "/dashboard/api?days=" in script\n\n\ndef test_dashboard_api_disables_http_caching() -> None:\n    source = (STATIC.parent / "dashboard.py").read_text(encoding="utf-8")\n    assert '"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"' in source\n    assert '"Pragma": "no-cache"' in source''',
    "dashboard live refresh regression",
)

replace_once(
    "CHANGELOG.md",
    "### Fixed\n\n",
    "### Fixed\n\n- Made the dashboard update continuously without page reloads by using non-overlapping live API polls, no-cache responses, in-place chart updates, and immediate catch-up when a background tab becomes visible again.\n",
    "changelog",
)
