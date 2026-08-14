const REFRESH_MS = 5000;
let state = null;
let timer = null;
let loadVersion = 0;

const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
};

const fmt = (n) => n == null ? "N/A" : n.toLocaleString();
const ms = (n) => n == null ? "-" : `${(+n).toFixed(+n < 10 ? 2 : 1)}ms`;
const time = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "-";

function setRequestFilter(value) {
  document.getElementById("request_id").value = value || "";
  load();
}

function populateProviders() {
  const select = document.getElementById("provider");
  const current = select.value;
  const seen = new Set();
  const providers = [];
  for (const ev of state.events || []) {
    if (ev.provider && !seen.has(ev.provider)) {
      seen.add(ev.provider);
      providers.push(ev.provider);
    }
  }
  for (const msg of state.messages || []) {
    if (msg.provider_name && !seen.has(msg.provider_name)) {
      seen.add(msg.provider_name);
      providers.push(msg.provider_name);
    }
  }
  providers.sort();
  select.innerHTML = '<option value="">All providers</option>' + providers.map((p) => `<option value="${p}">${p}</option>`).join("");
  if (current && seen.has(current)) select.value = current;
}

async function load() {
  const version = ++loadVersion;
  const q = new URLSearchParams({
    days: document.getElementById("days").value,
    limit: document.getElementById("limit").value,
    level: document.getElementById("level").value,
    provider: document.getElementById("provider").value,
    request_id: document.getElementById("request_id").value.trim(),
    view: document.getElementById("view").value,
  });
  const status = document.getElementById("status");
  status.textContent = "loading…";
  status.className = "status";
  try {
    const resp = await fetch("/logs/api?" + q.toString());
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const nextState = await resp.json();
    if (version !== loadVersion) return;
    state = nextState;
    populateProviders();
    renderEvents();
    renderMessages();
    status.textContent = "ok";
    status.className = "status fresh";
  } catch (err) {
    if (version !== loadVersion) return;
    status.textContent = "error: " + err.message;
    status.className = "status stale";
  }
  document.getElementById("updated").textContent = "updated " + new Date().toLocaleTimeString();
}

function levelTag(level) {
  const map = { error: "err", warning: "warn", info: "neutral", debug: "neutral" };
  return `tag ${map[level] || "neutral"}`;
}

function renderEvents() {
  const tbody = document.querySelector("#events tbody");
  const wrap = document.getElementById("events-wrap");
  tbody.innerHTML = "";
  const events = state.events || [];
  document.getElementById("event-count").textContent = `${events.length} event(s)`;
  const vis = document.getElementById("view").value;
  if (vis === "messages") { wrap.style.display = "none"; return; }
  wrap.style.display = "";
  for (const ev of events) {
    const tr = el("tr");
    tr.appendChild(el("td", null, time(ev.occurred_at)));
    tr.appendChild(el("td", null, null))
      .appendChild(el("span", levelTag(ev.level), ev.level || "-"));
    tr.appendChild(el("td", null, ev.source || "-"));
    tr.appendChild(el("td", null, ev.provider || "-"));
    tr.appendChild(el("td", null, ev.model || "-"));
    const msgTd = tr.appendChild(el("td", null, ev.message || "-"));
    msgTd.title = ev.message || "";
    const idTd = tr.appendChild(el("td", ev.request_id ? "clickable-id" : null, ev.request_id || ""));
    if (ev.request_id) idTd.addEventListener("click", (e) => { e.stopPropagation(); setRequestFilter(ev.request_id); });
    tr.addEventListener("click", () => toggleDetails(tr, ev));
    tbody.appendChild(tr);
  }
}

function renderMessages() {
  const tbody = document.querySelector("#messages tbody");
  const wrap = document.getElementById("messages-wrap");
  tbody.innerHTML = "";
  const messages = state.messages || [];
  document.getElementById("message-count").textContent = `${messages.length} message log(s)`;
  const vis = document.getElementById("view").value;
  if (vis === "events") { wrap.style.display = "none"; return; }
  wrap.style.display = "";
  for (const m of messages) {
    const tr = el("tr");
    tr.appendChild(el("td", null, time(m.occurred_at)));
    tr.appendChild(el("td", null, m.provider_name || "-"));
    tr.appendChild(el("td", null, m.model || "-"));
    tr.appendChild(el("td", null, null))
      .appendChild(el("span", "tag neutral", m.request_kind || "-"));
    const ok = !!m.success;
    const rk = m.response_kind || "";
    tr.appendChild(el("td", null, null))
      .appendChild(el("span", "tag " + (ok ? "ok" : "err"), (ok ? "success" : "failed") + (rk && rk !== "chat" ? " · " + rk : "")));
    tr.appendChild(el("td", null, fmt(m.total_tokens)));
    tr.appendChild(el("td", null, ms(m.latency_ms)));
    const idTd = tr.appendChild(el("td", m.request_id ? "clickable-id" : null, m.request_id || ""));
    if (m.request_id) idTd.addEventListener("click", (e) => { e.stopPropagation(); setRequestFilter(m.request_id); });
    const cell = tr.appendChild(el("td"));
    cell.appendChild(el("button", "btn small", "details"));
    tr.addEventListener("click", () => toggleDetails(tr, m, true));
    tbody.appendChild(tr);
  }
}

function toggleDetails(tr, item, isMessage = false) {
  if (tr.nextElementSibling && tr.nextElementSibling.classList.contains("detail-row")) {
    tr.nextElementSibling.remove();
    return;
  }
  const detail = el("tr", "detail-row");
  const cell = el("td");
  cell.colSpan = tr.cells.length;
  const actions = el("div", "detail-actions");
  const copy = el("button", "btn small", "copy json");
  copy.addEventListener("click", async (e) => {
    e.stopPropagation();
    const text = isMessage
      ? JSON.stringify(item, null, 2)
      : JSON.stringify({ level: item.level, source: item.source, provider: item.provider, model: item.model, request_id: item.request_id, message: item.message, details_json: item.details_json }, null, 2);
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = "copied";
      setTimeout(() => { copy.textContent = "copy json"; }, 1200);
    } catch (_) {
      copy.textContent = "copy failed";
      setTimeout(() => { copy.textContent = "copy json"; }, 1200);
    }
  });
  actions.appendChild(copy);
  const pre = el("pre", "detail-json");
  if (isMessage) {
    const parts = [];
    if (item.request_json) parts.push("=== REQUEST ===", item.request_json);
    if (item.response_json) parts.push("=== RESPONSE ===", item.response_json);
    if (item.error_json && item.error_json !== "{}") parts.push("=== ERROR ===", item.error_json);
    pre.textContent = parts.length ? parts.join("\n\n") : "(no body captured)";
  } else {
    const details = { level: item.level, source: item.source, provider: item.provider,
      model: item.model, request_id: item.request_id, message: item.message };
    if (item.details_json && item.details_json !== "{}") {
      try { details.details = JSON.parse(item.details_json); } catch (_) { details.details_raw = item.details_json; }
    }
    pre.textContent = JSON.stringify(details, null, 2);
  }
  cell.appendChild(actions);
  cell.appendChild(pre);
  detail.appendChild(cell);
  tr.after(detail);
}

document.getElementById("apply").addEventListener("click", load);
["view", "level", "provider", "days", "limit", "request_id"].forEach((id) => {
  document.getElementById(id).addEventListener("change", load);
});
document.getElementById("request_id").addEventListener("keydown", (e) => { if (e.key === "Enter") load(); });

document.getElementById("autorefresh").addEventListener("change", () => {
  if (document.getElementById("autorefresh").checked) { startRefresh(); load(); }
  else stopRefresh();
});

function startRefresh() { if (!timer) timer = setInterval(load, REFRESH_MS); }
function stopRefresh() { if (timer) { clearInterval(timer); timer = null; } }

load();
startRefresh();
