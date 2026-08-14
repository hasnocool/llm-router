const REFRESH_MS = 5000;
let state = null;
let charts = {};
let loadVersion = 0;

const fmt = (n) => n == null ? "N/A" : n.toLocaleString();
const ms = (n) => n == null ? "-" : `${n.toFixed(n < 10 ? 2 : 1)}ms`;
const pctText = (n) => n == null ? "N/A" : `${(n * 100).toFixed(n < 0.1 ? 1 : 0)}%`;
const money = (n, currency = "USD") => n == null ? "N/A" : new Intl.NumberFormat("en-US", {
  style: "currency",
  currency,
  maximumFractionDigits: 4,
}).format(n);
const pct = (used, remaining) => {
  if (used == null && remaining == null) return null;
  const total = (used || 0) + (remaining || 0);
  return total > 0 ? ((used || 0) / total) * 100 : 0;
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function tick() {
  document.getElementById("updated").textContent =
    "updated " + new Date().toLocaleTimeString();
}

async function load() {
  const version = ++loadVersion;
  const days = document.getElementById("days").value;
  document.getElementById("status").textContent = "loading…";
  document.getElementById("status").className = "status";
  try {
    const resp = await fetch(`/dashboard/api?days=${days}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const nextState = await resp.json();
    if (version !== loadVersion) return;
    state = nextState;
    render();
    document.getElementById("status").textContent = "ok";
    document.getElementById("status").className = "status fresh";
  } catch (err) {
    if (version !== loadVersion) return;
    document.getElementById("status").textContent = "error: " + err.message;
    document.getElementById("status").className = "status stale";
  }
  tick();
}

function render() {
  renderSummary();
  renderCharts();
  renderProviders();
  renderMatrix();
  renderModels();
  renderKinds();
  renderAnalytics();
  renderEvents();
}

function renderSummary() {
  const s = state.summary || {};
  const totalCalls = s.calls_today || 0;
  const success = s.success_today || 0;
  const failed = s.failed_today || 0;
  const elts = [
    ["Calls today", fmt(totalCalls), ""],
    ["Successful", fmt(success), "ok"],
    ["Failed", fmt(failed), failed > 0 ? "err" : ""],
    ["Tokens today", fmt(s.tokens_today), ""],
    ["Eligible providers", `${s.providers_eligible || 0} / ${s.providers_configured || 0}`, ""],
    ["Calls remaining", fmt(s.calls_remaining), ""],
    ["Tokens remaining", fmt(s.tokens_remaining), ""],
    ["Rate-limit warnings", fmt(s.rate_limit_warnings), (s.rate_limit_warnings || 0) > 0 ? "warn" : "ok"],
  ];
  const wrap = document.getElementById("summary");
  wrap.innerHTML = "";
  for (const [label, value, cls] of elts) {
    const card = el("div", "card " + cls);
    card.appendChild(el("div", "label", label));
    card.appendChild(el("div", "value", String(value)));
    wrap.appendChild(card);
  }
}

function series(providers, prop) {
  const dates = new Set();
  for (const p of Object.values(providers)) {
    for (const h of p.history) dates.add(h.date);
  }
  const sorted = [...dates].sort();
  return sorted.map((date) => {
    const row = {};
    for (const [name, p] of Object.entries(providers)) {
      const hit = p.history.find((h) => h.date === date);
      row[name] = hit ? hit[prop] : 0;
    }
    return { date, row };
  });
}

function makeChart(canvasId, title, labels, datasets, opts = {}) {
  const canvas = document.getElementById(canvasId);
  if (charts[canvasId]) charts[canvasId].destroy();
  charts[canvasId] = new Chart(canvas, {
    type: "bar",
    data: { labels, datasets },
    options: Object.assign({
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { labels: { color: "#8b98a5", boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } },
        y: { ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" }, beginAtZero: true },
      },
    }, opts),
  });
}

function renderCharts() {
  const providers = state.providers || {};
  const names = Object.keys(providers);
  const palette = ["#4c9aff", "#3fb950", "#d29922", "#f85149", "#b77bf1", "#39c5cf"];

  const calls = series(providers, "calls");
  makeChart("chart-calls", null, calls.map((c) => c.date),
    names.map((n, i) => ({
      label: n,
      data: calls.map((c) => c.row[n]),
      backgroundColor: palette[i % palette.length],
    })));

  const tokens = series(providers, "total_tokens");
  makeChart("chart-tokens", null, tokens.map((c) => c.date),
    names.map((n, i) => ({
      label: n,
      data: tokens.map((c) => c.row[n]),
      backgroundColor: palette[i % palette.length],
    })));

  const latency = series(providers, "latency_p50_ms");
  const labels = latency.map((c) => c.date);
  makeChart("chart-latency", null, labels,
    names.map((n, i) => ({
      type: "line",
      label: n,
      data: latency.map((c) => c.row[n]),
      borderColor: palette[i % palette.length],
      backgroundColor: "transparent",
      tension: 0.3,
    })), { y: { beginAtZero: true } });
}

function bar(used, remaining) {
  const wrap = el("div", "bar wrap");
  const fill = el("div", "bar");
  fill.style.width = "20%";
  fill.style.background = "var(--accent)";
  const p = pct(used, remaining);
  if (p != null) {
    fill.style.width = Math.min(100, Math.max(2, p)).toFixed(0) + "%";
    fill.style.background = p > 90 ? "var(--err)" : p > 70 ? "var(--warn)" : "var(--ok)";
  } else {
    fill.style.background = "var(--muted)";
  }
  wrap.appendChild(fill);
  return wrap;
}

function renderProviders() {
  const tbody = document.querySelector("#providers tbody");
  tbody.innerHTML = "";
  for (const p of Object.values(state.providers || {})) {
    const tr = el("tr");
    tr.appendChild(el("td", null, p.name));
    const ok = p.available;
    tr.appendChild(el("td", null, null))
      .appendChild(el("span", "tag " + (ok ? "ok" : "err"), ok ? "available" : "down"));
    tr.appendChild(el("td", null, fmt(p.model_count)));
    const callsTd = tr.appendChild(el("td"));
    callsTd.appendChild(bar(p.daily_calls_used, p.daily_calls_remaining));
    callsTd.appendChild(document.createTextNode(
      `${fmt(p.daily_calls_used)} / ${fmt(p.daily_calls_remaining)}`));
    const tokensTd = tr.appendChild(el("td"));
    tokensTd.appendChild(bar(p.daily_tokens_used, p.daily_tokens_remaining));
    tokensTd.appendChild(document.createTextNode(
      `${fmt(p.daily_tokens_used)} / ${fmt(p.daily_tokens_remaining)}`));
    const rl = p.rate_limit_remaining;
    const rlTd = tr.appendChild(el("td"));
    if (rl == null) {
      rlTd.textContent = "unknown";
    } else {
      const cls = rl === 0 ? "err" : rl < 100 ? "warn" : "ok";
      const reset = p.rate_limit_reset ? new Date(p.rate_limit_reset * 1000).toLocaleTimeString() : "";
      rlTd.appendChild(el("span", "tag " + cls, fmt(rl)));
      if (reset) rlTd.appendChild(document.createTextNode(" reset " + reset));
    }
    tr.appendChild(el("td", null, `${ms(p.latency_p50_ms)} / ${ms(p.latency_p99_ms)}`));
    const err = p.last_error || "";
    tr.appendChild(el("td", null, err ? "⚠ " + err : "-"));
    tbody.appendChild(tr);
  }
}

function renderMatrix() {
  const tbody = document.querySelector("#matrix tbody");
  tbody.innerHTML = "";
  const rows = state.matrix || [];
  if (rows.length === 0) return;
  const ordered = [...rows].sort((a, b) =>
    (a.routing?.eligible === b.routing?.eligible ? 0 : a.routing?.eligible ? -1 : 1) ||
    (b.routing?.dynamic_score || 0) - (a.routing?.dynamic_score || 0));
  for (const item of ordered) {
    const r = item.routing || {};
    const tr = el("tr");
    tr.appendChild(el("td", null, item.configured_provider || item.id || "-"));
    tr.appendChild(el("td", null, r.free_class || "-"));
    tr.appendChild(el("td", null, fmt(r.static_score)));
    tr.appendChild(el("td", null, (r.dynamic_score ?? "-").toLocaleString()));
    tr.appendChild(el("td", null, null))
      .appendChild(el("span", "tag " + (r.eligible ? "ok" : "err"), r.eligible ? "eligible" : "blocked"));
    const reasons = el("td", "reasons-chips");
    for (const reason of r.reasons || []) reasons.appendChild(el("span", null, reason));
    tr.appendChild(reasons);
    tbody.appendChild(tr);
  }
}

function renderModels() {
  const tbody = document.querySelector("#models tbody");
  tbody.innerHTML = "";
  for (const m of state.models || []) {
    const tr = el("tr");
    tr.appendChild(el("td", null, m.id));
    tr.appendChild(el("td", null, m.owned_by));
    tbody.appendChild(tr);
  }
}

function renderKinds() {
  const kb = state.summary?.kind_breakdown || { request: {}, response: {} };
  const canvas = document.getElementById("chart-kinds");
  if (charts["chart-kinds"]) charts["chart-kinds"].destroy();
  const labels = [...new Set([...Object.keys(kb.request), ...Object.keys(kb.response)])];
  charts["chart-kinds"] = new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "request", data: labels.map((l) => kb.request[l] || 0), backgroundColor: "#4c9aff" },
        { label: "response", data: labels.map((l) => kb.response[l] || 0), backgroundColor: "#b77bf1" },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: { labels: { color: "#8b98a5" } } },
      scales: {
        x: { ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } },
        y: { ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" }, beginAtZero: true },
      },
    },
  });
}

function renderAnalytics() {
  const analytics = state.analytics || {};
  const summary = analytics.summary || {};
  const providers = analytics.providers || {};
  const timeline = analytics.timeline || [];
  const taskBreakdown = summary.task_breakdown || {};
  const providerNames = Object.keys(providers);
  const currency = summary.currency || "USD";

  const cards = document.getElementById("analytics-summary");
  cards.innerHTML = "";
  const items = [
    ["Attempts", fmt(summary.attempts), ""],
    ["Success rate", pctText(summary.success_rate), summary.success_rate != null && summary.success_rate < 0.9 ? "warn" : "ok"],
    ["Failover rate", pctText(summary.failover_rate), summary.failover_rate != null && summary.failover_rate > 0.15 ? "warn" : "ok"],
    ["Estimated cost", money(summary.estimated_cost_usd, currency), ""],
    ["Tokens", fmt(summary.total_tokens), ""],
    ["Providers", fmt(summary.providers), ""],
  ];
  for (const [label, value, cls] of items) {
    const card = el("div", "card " + cls);
    card.appendChild(el("div", "label", label));
    card.appendChild(el("div", "value money", String(value)));
    cards.appendChild(card);
  }

  const alerts = document.getElementById("analytics-alerts");
  alerts.innerHTML = "";
  const list = analytics.alerts || [];
  if (list.length === 0) {
    alerts.appendChild(el("div", "alert-item", "No active analytics alerts"));
  } else {
    for (const alert of list) {
      const item = el("div", `alert-item ${alert.severity || "warning"}`);
      item.appendChild(el("div", null, `${alert.message || alert.metric || "alert"}`));
      item.appendChild(el("div", "meta", `${alert.metric || "metric"} · ${alert.provider || "overall"} · threshold ${pctText(alert.threshold)} · observed ${pctText(alert.value)}`));
      alerts.appendChild(item);
    }
  }

  const rows = document.querySelector("#analytics-providers tbody");
  rows.innerHTML = "";
  const ordered = providerNames.sort((a, b) => (providers[b].estimated_cost_usd || 0) - (providers[a].estimated_cost_usd || 0));
  for (const name of ordered) {
    const p = providers[name];
    const tr = el("tr");
    tr.appendChild(el("td", null, name));
    tr.appendChild(el("td", null, fmt(p.attempts)));
    tr.appendChild(el("td", null, pctText(p.success_rate)));
    tr.appendChild(el("td", null, pctText(p.failover_rate)));
    tr.appendChild(el("td", null, fmt(p.total_tokens)));
    tr.appendChild(el("td", "money", money(p.estimated_cost_usd, p.currency || currency)));
    rows.appendChild(tr);
  }

  const costLabels = ordered;
  makeChart("chart-cost", null, costLabels,
    [{
      label: `Estimated cost (${currency})`,
      data: costLabels.map((name) => providers[name].estimated_cost_usd || 0),
      backgroundColor: "#4c9aff",
    }],
    { scales: { y: { beginAtZero: true, ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } }, x: { ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } } } }
  );

  makeChart("chart-success", null, costLabels,
    [{
      label: "Success rate",
      data: costLabels.map((name) => providers[name].success_rate == null ? 0 : providers[name].success_rate * 100),
      backgroundColor: "#3fb950",
    }],
    { scales: { y: { beginAtZero: true, max: 100, ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } }, x: { ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } } } }
  );

  const days = timeline.map((row) => row.day);
  makeChart("chart-activity", null, days,
    [
      { label: "Attempts", data: timeline.map((row) => row.attempts || 0), backgroundColor: "#4c9aff" },
      { label: "Failures", data: timeline.map((row) => row.failures || 0), backgroundColor: "#f85149" },
      { label: "Failovers", data: timeline.map((row) => row.failovers || 0), backgroundColor: "#d29922" },
    ],
    { scales: { y: { beginAtZero: true, ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } }, x: { ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } } } }
  );

  const errorLevels = summary.error_levels || {};
  const errorLabels = Object.keys(errorLevels);
  makeChart("chart-errors", null, errorLabels,
    [{
      label: "Events",
      data: errorLabels.map((name) => errorLevels[name] || 0),
      backgroundColor: errorLabels.map((name) => name === "error" ? "#f85149" : name === "warning" ? "#d29922" : "#4c9aff"),
    }],
    { scales: { y: { beginAtZero: true, ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } }, x: { ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } } } }
  );

  const taskLabels = Object.keys(taskBreakdown);
  makeChart("chart-tasks", null, taskLabels,
    [{
      label: "Tasks",
      data: taskLabels.map((name) => taskBreakdown[name] || 0),
      backgroundColor: "#b77bf1",
    }],
    { scales: { y: { beginAtZero: true, ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } }, x: { ticks: { color: "#8b98a5" }, grid: { color: "#2d3646" } } } }
  );
}

function renderEvents() {
  const tbody = document.querySelector("#events tbody");
  tbody.innerHTML = "";
  for (const ev of state.recent_events || []) {
    const tr = el("tr");
    const time = ev.occurred_at ? new Date(ev.occurred_at * 1000).toLocaleTimeString() : "-";
    tr.appendChild(el("td", null, time));
    tr.appendChild(el("td", null, ev.provider_name || "-"));
    tr.appendChild(el("td", null, ev.model || "-"));
    tr.appendChild(el("td", null, null))
      .appendChild(el("span", "tag neutral", ev.request_kind || "-"));
    const rk = ev.response_kind || "-";
    tr.appendChild(el("td", null, null))
      .appendChild(el("span", "tag " + (rk === "tool_call" ? "warn" : "neutral"), rk));
    tr.appendChild(el("td", null, (ev.stream ? "stream" : "sync") + " / " + (ev.explicit ? "explicit" : "auto")));
    tr.appendChild(el("td", null, fmt(ev.failover_index)));
    const ok = !!ev.success;
    tr.appendChild(el("td", null, null))
      .appendChild(el("span", "tag " + (ok ? "ok" : "err"), ok ? "ok" : "failed"));
    tr.appendChild(el("td", null, fmt(ev.total_tokens)));
    tr.appendChild(el("td", null, ms(ev.latency_ms)));
    tbody.appendChild(tr);
  }
}

document.getElementById("days").addEventListener("change", load);
document.getElementById("autorefresh").addEventListener("change", () => {
  if (document.getElementById("autorefresh").checked) {
    startRefresh();
    load();
  } else {
    stopRefresh();
  }
});

let timer = null;
function startRefresh() { if (!timer) timer = setInterval(load, REFRESH_MS); }
function stopRefresh() { if (timer) { clearInterval(timer); timer = null; } }

load();
startRefresh();
