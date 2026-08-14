// src/llm_router/static/dashboard.js
const REFRESH_MS = 5000;
const CHART_COLORS = ["#6ea8fe", "#4bd28f", "#f0b45a", "#ff6b72", "#b69cff", "#57d1d8", "#d38cf2"];
const GRID_COLOR = "#1b2633";
const MUTED_COLOR = "#8190a3";

let state = null;
let charts = {};
let loadVersion = 0;
let timer = null;
let activeRequest = null;
let refreshInFlight = false;
let toastTimer = null;

const fmt = (value) => value == null ? "N/A" : Number(value).toLocaleString();
const ms = (value) => value == null ? "—" : `${Number(value).toFixed(value < 10 ? 2 : 1)} ms`;
const pctText = (value) => value == null ? "N/A" : `${(Number(value) * 100).toFixed(value < 0.1 ? 1 : 0)}%`;
const money = (value, currency = "USD") => value == null ? "N/A" : new Intl.NumberFormat("en-US", {
  style: "currency",
  currency,
  maximumFractionDigits: 4,
}).format(value);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function ratio(used, remaining) {
  if (used == null && remaining == null) return null;
  const total = Number(used || 0) + Number(remaining || 0);
  return total > 0 ? Number(used || 0) / total : 0;
}

function showToast(message, kind = "") {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className = `toast visible ${kind}`.trim();
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 2400);
}

function setConnection(mode, text) {
  document.getElementById("status").textContent = text;
  document.getElementById("status-dot").className = `connection-dot ${mode}`;
}

function setLoading(isLoading) {
  const button = document.getElementById("refresh-now");
  button.disabled = isLoading;
  button.textContent = isLoading ? "Refreshing…" : "Refresh";
}

async function load({ notify = false, force = false } = {}) {
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
}

function render() {
  document.getElementById("strategy").textContent = state.strategy || "unknown";
  renderSummary();
  renderOverviewCharts();
  renderProviders();
  renderMatrix();
  renderModels();
  renderKinds();
  renderAnalytics();
  renderEvents();
}

function metricCard(label, value, detail = "", cls = "") {
  const card = el("div", `metric-card ${cls}`.trim());
  card.appendChild(el("div", "label", label));
  card.appendChild(el("div", "value", String(value)));
  if (detail) card.appendChild(el("div", "detail", detail));
  return card;
}

function renderSummary() {
  const summary = state.summary || {};
  const analytics = state.analytics?.summary || {};
  const totalCalls = Number(summary.calls_today || 0);
  const successful = Number(summary.success_today || 0);
  const failed = Number(summary.failed_today || 0);
  const successRate = totalCalls > 0 ? successful / totalCalls : 1;
  const failovers = Number(analytics.failovers || 0);
  const rateWarnings = Number(summary.rate_limit_warnings || 0);
  const alerts = state.analytics?.alerts || [];

  const cards = [
    ["Requests today", fmt(totalCalls), `${fmt(failed)} failed`, failed > 0 ? "warn" : ""],
    ["Success rate", pctText(successRate), `${fmt(successful)} successful`, successRate < .9 ? "warn" : "ok"],
    ["Tokens today", fmt(summary.tokens_today || 0), `${fmt(summary.tokens_remaining)} remaining`, ""],
    ["Eligible routes", `${summary.providers_eligible || 0}/${summary.providers_configured || 0}`, `${rateWarnings} rate-limit warnings`, rateWarnings ? "warn" : "ok"],
    ["Estimated cost", money(analytics.estimated_cost_usd, analytics.currency || "USD"), `${fmt(analytics.total_tokens || 0)} analyzed tokens`, ""],
    ["Failovers", fmt(failovers), `${pctText(analytics.failover_rate)} of attempts`, failovers ? "warn" : "ok"],
  ];

  const wrap = document.getElementById("summary");
  wrap.innerHTML = "";
  for (const [label, value, detail, cls] of cards) wrap.appendChild(metricCard(label, value, detail, cls));

  const health = document.getElementById("health-badge");
  if (alerts.some((item) => item.severity === "critical") || successRate < .8) {
    health.className = "health-badge err";
    health.textContent = "Needs attention";
  } else if (alerts.length || failed > 0 || rateWarnings > 0) {
    health.className = "health-badge warn";
    health.textContent = "Degraded, still routing";
  } else {
    health.className = "health-badge ok";
    health.textContent = "Operating normally";
  }
}

function series(providers, property) {
  const dates = new Set();
  for (const provider of Object.values(providers)) {
    for (const history of provider.history || []) dates.add(history.date);
  }
  const sorted = [...dates].sort();
  return sorted.map((date) => {
    const row = {};
    for (const [name, provider] of Object.entries(providers)) {
      const hit = (provider.history || []).find((entry) => entry.date === date);
      row[name] = hit ? Number(hit[property] || 0) : 0;
    }
    return { date, row };
  });
}

function chartOptions(extra = {}) {
  const scales = extra.scales || {};
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { intersect: false, mode: "index" },
    plugins: {
      legend: { position: "bottom", labels: { color: MUTED_COLOR, boxWidth: 9, boxHeight: 9, padding: 14, usePointStyle: true } },
      tooltip: { backgroundColor: "#182230", borderColor: "#253140", borderWidth: 1, titleColor: "#edf3f8", bodyColor: "#c6d0dc" },
    },
    scales: {
      x: Object.assign({ ticks: { color: MUTED_COLOR, maxRotation: 0 }, grid: { color: GRID_COLOR } }, scales.x || {}),
      y: Object.assign({ ticks: { color: MUTED_COLOR }, grid: { color: GRID_COLOR }, beginAtZero: true }, scales.y || {}),
    },
  };
}

function makeChart(canvasId, type, labels, datasets, extra = {}) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || typeof Chart === "undefined") return;
  if (charts[canvasId]) {
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
  });
}

function renderOverviewCharts() {
  const providers = state.providers || {};
  const names = Object.keys(providers);

  const calls = series(providers, "calls");
  makeChart("chart-calls", "bar", calls.map((item) => item.date), names.map((name, index) => ({
    label: name,
    data: calls.map((item) => item.row[name]),
    backgroundColor: CHART_COLORS[index % CHART_COLORS.length],
    borderRadius: 4,
    maxBarThickness: 24,
  })));

  const tokens = series(providers, "total_tokens");
  makeChart("chart-tokens", "bar", tokens.map((item) => item.date), names.map((name, index) => ({
    label: name,
    data: tokens.map((item) => item.row[name]),
    backgroundColor: CHART_COLORS[index % CHART_COLORS.length],
    borderRadius: 4,
    maxBarThickness: 20,
  })));

  const latency = series(providers, "latency_p50_ms");
  makeChart("chart-latency", "line", latency.map((item) => item.date), names.map((name, index) => ({
    label: name,
    data: latency.map((item) => item.row[name]),
    borderColor: CHART_COLORS[index % CHART_COLORS.length],
    backgroundColor: CHART_COLORS[index % CHART_COLORS.length],
    pointRadius: 2,
    pointHoverRadius: 4,
    borderWidth: 2,
    tension: .28,
  })));
}

function quotaFill(used, remaining) {
  const fill = el("div", "quota-fill");
  const usage = ratio(used, remaining);
  if (usage == null) {
    fill.style.width = "18%";
    fill.style.background = "var(--muted)";
    return fill;
  }
  const percent = Math.min(100, Math.max(2, usage * 100));
  fill.style.width = `${percent.toFixed(0)}%`;
  fill.style.background = usage >= .9 ? "var(--err)" : usage >= .7 ? "var(--warn)" : "var(--ok)";
  return fill;
}

function quotaRow(label, used, remaining) {
  const row = el("div", "quota-row");
  row.appendChild(el("span", null, label));
  const track = row.appendChild(el("div", "quota-track"));
  track.appendChild(quotaFill(used, remaining));
  row.appendChild(el("span", null, `${fmt(used)} / ${fmt(remaining)}`));
  return row;
}

function providerState(provider) {
  if (provider.in_backoff) return ["backoff", "warn", "cooldown"];
  if (!provider.available) return ["down", "err", "unavailable"];
  if (provider.rate_limit_remaining === 0) return ["backoff", "warn", "rate limited"];
  return ["", "ok", "available"];
}

function renderProviders() {
  const wrap = document.getElementById("providers");
  wrap.innerHTML = "";
  const providers = Object.values(state.providers || {}).sort((a, b) => {
    const priority = (provider) => provider.in_backoff ? 1 : !provider.available ? 2 : 0;
    return priority(a) - priority(b) || String(a.name).localeCompare(String(b.name));
  });

  if (!providers.length) {
    wrap.appendChild(el("div", "empty-state", "No providers configured"));
    return;
  }

  for (const provider of providers) {
    const [cardState, tagState, label] = providerState(provider);
    const card = el("article", `provider-card ${cardState}`.trim());
    const head = card.appendChild(el("div", "provider-card-head"));
    const identity = head.appendChild(el("div"));
    identity.appendChild(el("div", "provider-name", provider.name));
    const secondary = provider.in_backoff && provider.backoff_until
      ? `retry after ${new Date(provider.backoff_until * 1000).toLocaleTimeString()}`
      : `${fmt(provider.model_count)} model${provider.model_count === 1 ? "" : "s"}`;
    identity.appendChild(el("div", "provider-sub", secondary));
    head.appendChild(el("span", `tag ${tagState}`, label));

    const stats = card.appendChild(el("div", "provider-stats"));
    const mini = (labelText, valueText) => {
      const item = stats.appendChild(el("div", "mini-stat"));
      item.appendChild(el("div", "mini-label", labelText));
      item.appendChild(el("div", "mini-value", valueText));
    };
    mini("p50", ms(provider.latency_p50_ms));
    mini("p99", ms(provider.latency_p99_ms));
    mini("Failures", fmt(provider.consecutive_failures || 0));
    mini("Error class", provider.last_error_class || "none");

    const quotas = card.appendChild(el("div", "quota-group"));
    quotas.appendChild(quotaRow("Calls", provider.daily_calls_used, provider.daily_calls_remaining));
    quotas.appendChild(quotaRow("Tokens", provider.daily_tokens_used, provider.daily_tokens_remaining));

    const rate = provider.rate_limit_remaining;
    const reset = provider.rate_limit_reset ? new Date(provider.rate_limit_reset * 1000).toLocaleTimeString() : "unknown";
    const rateText = rate == null ? "Rate limit unknown" : `Rate limit ${fmt(rate)} · reset ${reset}`;
    const error = provider.last_error || "";
    const footer = card.appendChild(el("div", `provider-error ${error ? "has-error" : ""}`.trim(), error || rateText));
    footer.title = error || rateText;
    wrap.appendChild(card);
  }
}

function renderMatrix() {
  const tbody = document.querySelector("#matrix tbody");
  tbody.innerHTML = "";
  const rows = [...(state.matrix || [])].sort((a, b) =>
    (a.routing?.eligible === b.routing?.eligible ? 0 : a.routing?.eligible ? -1 : 1) ||
    Number(b.routing?.dynamic_score || 0) - Number(a.routing?.dynamic_score || 0));

  const eligible = rows.filter((item) => item.routing?.eligible).length;
  document.getElementById("matrix-count").textContent = `${eligible}/${rows.length} eligible`;

  if (!rows.length) {
    const tr = tbody.appendChild(el("tr"));
    const td = tr.appendChild(el("td", "empty-state", "No routing matrix is active for this strategy"));
    td.colSpan = 6;
    return;
  }

  for (const item of rows) {
    const route = item.routing || {};
    const tr = el("tr");
    tr.appendChild(el("td", null, item.configured_provider || item.id || "—"));
    tr.appendChild(el("td", null, route.free_class || "—"));
    tr.appendChild(el("td", "score", fmt(route.static_score)));
    tr.appendChild(el("td", "score", route.dynamic_score == null ? "—" : Number(route.dynamic_score).toFixed(2)));
    const stateCell = tr.appendChild(el("td"));
    stateCell.appendChild(el("span", `tag ${route.eligible ? "ok" : "err"}`, route.eligible ? "eligible" : "blocked"));
    const reasons = tr.appendChild(el("td"));
    const chips = reasons.appendChild(el("div", "reasons-chips"));
    const reasonList = route.reasons || [];
    if (!reasonList.length) chips.appendChild(el("span", "reason-chip", "policy clear"));
    for (const reason of reasonList) chips.appendChild(el("span", "reason-chip", reason));
    tbody.appendChild(tr);
  }
}

function renderModels() {
  const wrap = document.getElementById("models");
  wrap.innerHTML = "";
  const models = state.models || [];
  document.getElementById("model-count").textContent = `${fmt(models.length)} visible`;
  if (!models.length) {
    wrap.appendChild(el("div", "empty-state", "No advertised models"));
    return;
  }
  for (const model of models) {
    const item = wrap.appendChild(el("div", "model-item"));
    const id = item.appendChild(el("span", "model-id", model.id));
    id.title = model.id;
    item.appendChild(el("span", "model-owner", model.owned_by || "router"));
  }
}

function renderKinds() {
  const breakdown = state.summary?.kind_breakdown || { request: {}, response: {} };
  const labels = [...new Set([...Object.keys(breakdown.request || {}), ...Object.keys(breakdown.response || {})])];
  makeChart("chart-kinds", "bar", labels, [
    { label: "Request", data: labels.map((label) => breakdown.request?.[label] || 0), backgroundColor: CHART_COLORS[0], borderRadius: 4, maxBarThickness: 28 },
    { label: "Response", data: labels.map((label) => breakdown.response?.[label] || 0), backgroundColor: CHART_COLORS[4], borderRadius: 4, maxBarThickness: 28 },
  ]);
}

function renderAnalytics() {
  const analytics = state.analytics || {};
  const summary = analytics.summary || {};
  const providers = analytics.providers || {};
  const timeline = analytics.timeline || [];
  const taskBreakdown = summary.task_breakdown || {};
  const currency = summary.currency || "USD";

  const summaryWrap = document.getElementById("analytics-summary");
  summaryWrap.innerHTML = "";
  const cards = [
    ["Attempts", fmt(summary.attempts || 0), `${fmt(summary.failures || 0)} failures`, ""],
    ["Success rate", pctText(summary.success_rate), "all providers", summary.success_rate != null && summary.success_rate < .9 ? "warn" : "ok"],
    ["Failover rate", pctText(summary.failover_rate), `${fmt(summary.failovers || 0)} failovers`, summary.failover_rate != null && summary.failover_rate > .15 ? "warn" : "ok"],
    ["Estimated cost", money(summary.estimated_cost_usd, currency), currency, ""],
    ["Analyzed tokens", fmt(summary.total_tokens || 0), "prompt + completion", ""],
    ["Providers", fmt(summary.providers || 0), "with analytics data", ""],
  ];
  for (const [label, value, detail, cls] of cards) summaryWrap.appendChild(metricCard(label, value, detail, cls));

  const alertsWrap = document.getElementById("analytics-alerts");
  alertsWrap.innerHTML = "";
  const alerts = analytics.alerts || [];
  if (!alerts.length) {
    const item = alertsWrap.appendChild(el("div", "alert-item none"));
    item.appendChild(el("div", "alert-message", "No active reliability or failover alerts"));
    item.appendChild(el("div", "alert-meta", "threshold checks are clear"));
  } else {
    for (const alert of alerts) {
      const item = alertsWrap.appendChild(el("div", `alert-item ${alert.severity || "warning"}`));
      item.appendChild(el("div", "alert-message", alert.message || alert.metric || "Analytics alert"));
      item.appendChild(el("div", "alert-meta", `${alert.provider || "overall"} · ${alert.metric || "metric"} · observed ${pctText(alert.value)}`));
    }
  }

  const ordered = Object.keys(providers).sort((a, b) => Number(providers[b].attempts || 0) - Number(providers[a].attempts || 0));
  const tbody = document.querySelector("#analytics-providers tbody");
  tbody.innerHTML = "";
  for (const name of ordered) {
    const provider = providers[name];
    const tr = el("tr");
    tr.appendChild(el("td", null, name));
    tr.appendChild(el("td", null, fmt(provider.attempts)));
    tr.appendChild(el("td", null, pctText(provider.success_rate)));
    tr.appendChild(el("td", null, pctText(provider.failover_rate)));
    tr.appendChild(el("td", null, fmt(provider.total_tokens)));
    tr.appendChild(el("td", "money", money(provider.estimated_cost_usd, provider.currency || currency)));
    tbody.appendChild(tr);
  }

  makeChart("chart-cost", "bar", ordered, [{
    label: `Estimated cost (${currency})`,
    data: ordered.map((name) => providers[name].estimated_cost_usd || 0),
    backgroundColor: CHART_COLORS[0],
    borderRadius: 4,
    maxBarThickness: 34,
  }]);

  makeChart("chart-success", "bar", ordered, [{
    label: "Success rate",
    data: ordered.map((name) => providers[name].success_rate == null ? 0 : providers[name].success_rate * 100),
    backgroundColor: CHART_COLORS[1],
    borderRadius: 4,
    maxBarThickness: 34,
  }], { scales: { y: { max: 100, ticks: { callback: (value) => `${value}%` } } } });

  makeChart("chart-activity", "line", timeline.map((row) => row.day), [
    { label: "Attempts", data: timeline.map((row) => row.attempts || 0), borderColor: CHART_COLORS[0], backgroundColor: CHART_COLORS[0], tension: .25, pointRadius: 2 },
    { label: "Failures", data: timeline.map((row) => row.failures || 0), borderColor: CHART_COLORS[3], backgroundColor: CHART_COLORS[3], tension: .25, pointRadius: 2 },
    { label: "Failovers", data: timeline.map((row) => row.failovers || 0), borderColor: CHART_COLORS[2], backgroundColor: CHART_COLORS[2], tension: .25, pointRadius: 2 },
  ]);

  const errorLevels = summary.error_levels || {};
  const errorLabels = Object.keys(errorLevels);
  makeChart("chart-errors", "bar", errorLabels, [{
    label: "Events",
    data: errorLabels.map((name) => errorLevels[name] || 0),
    backgroundColor: errorLabels.map((name) => name === "error" ? CHART_COLORS[3] : name === "warning" ? CHART_COLORS[2] : CHART_COLORS[0]),
    borderRadius: 4,
    maxBarThickness: 34,
  }]);

  const taskLabels = Object.keys(taskBreakdown);
  makeChart("chart-tasks", "bar", taskLabels, [{
    label: "Tasks",
    data: taskLabels.map((name) => taskBreakdown[name] || 0),
    backgroundColor: CHART_COLORS[4],
    borderRadius: 4,
    maxBarThickness: 34,
  }]);
}

function renderEvents() {
  const tbody = document.querySelector("#events tbody");
  tbody.innerHTML = "";
  const events = state.recent_events || [];
  if (!events.length) {
    const tr = tbody.appendChild(el("tr"));
    const td = tr.appendChild(el("td", "empty-state", "No recent routing events"));
    td.colSpan = 6;
    return;
  }

  for (const event of events) {
    const tr = el("tr");
    const occurred = event.occurred_at ? new Date(event.occurred_at * 1000).toLocaleTimeString() : "—";
    tr.appendChild(el("td", null, occurred));

    const route = tr.appendChild(el("td", "route-cell"));
    route.appendChild(el("div", "route-primary", event.provider_name || "router"));
    route.appendChild(el("div", "route-secondary", event.model || "—"));

    const mode = tr.appendChild(el("td"));
    mode.appendChild(el("span", "tag neutral", event.explicit ? "explicit" : "auto"));
    if (event.stream) mode.appendChild(document.createTextNode(" stream"));

    const result = tr.appendChild(el("td"));
    result.appendChild(el("span", `tag ${event.success ? "ok" : "err"}`, event.success ? (event.response_kind || "ok") : (event.error_class || "failed")));
    if (Number(event.failover_index || 0) > 0) result.appendChild(document.createTextNode(` · failover ${event.failover_index}`));

    tr.appendChild(el("td", null, fmt(event.total_tokens)));
    tr.appendChild(el("td", null, ms(event.latency_ms)));
    tbody.appendChild(tr);
  }
}

function startRefresh() {
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
}

function setupNavigation() {
  const links = [...document.querySelectorAll(".side-link")];
  const sections = links.map((link) => document.getElementById(link.dataset.section)).filter(Boolean);
  if (!("IntersectionObserver" in window)) return;
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    for (const link of links) link.classList.toggle("active", link.dataset.section === visible.target.id);
  }, { rootMargin: "-20% 0px -65% 0px", threshold: [0, .1, .4] });
  for (const section of sections) observer.observe(section);
}

document.getElementById("days").addEventListener("change", () => load({ notify: true, force: true }));
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
startRefresh();
