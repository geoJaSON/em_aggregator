"use strict";

const SEV = ["info", "minor", "moderate", "severe", "extreme"];
const GLYPH = {
  weather: "⛈️", flood: "🌊", power: "⚡", comms: "📶", roads: "🚧",
  fire: "🔥", seismic: "〰️", tropical: "🌀", transport: "✈️", shelter: "🏠", other: "📍",
};
const CHANGE_LABEL = { new: "New", escalated: "Escalated", deescalated: "Eased", ended: "Cleared", expired: "Expired" };
const RECENT_MS = 60 * 60 * 1000;
const LIST_PAGE = 300;
const CONTEXT_KINDS = new Set(["fema_declaration", "nhc_cone", "outlook"]);
const CAT_ORDER = ["weather", "flood", "tropical", "power", "comms", "roads", "transport", "fire", "seismic", "shelter", "other"];

const $ = (sel) => document.querySelector(sel);
const state = {
  config: null,
  features: [],
  summary: null,
  sources: [],
  timeline: [],
  stateRollup: [],
  stateFilter: load("stateFilter", ""),
  listLimit: LIST_PAGE,
  hidden: new Set(load("hidden", [])),
  minSev: Number(load("minSev", 0)),
  search: "",
  selected: null,
  expanded: null,
  openUid: null,
  tab: "events",
  placing: false,
  pendingLatLng: null,
};

// --- helpers ---------------------------------------------------------------------------------------

function load(key, fallback) {
  try {
    const v = localStorage.getItem("emagg." + key);
    return v == null ? fallback : JSON.parse(v);
  } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem("emagg." + key, JSON.stringify(value)); } catch { /* storage unavailable */ }
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function safeUrl(u) {
  if (!u) return null;
  try {
    const x = new URL(u, location.href);
    return x.protocol === "http:" || x.protocol === "https:" ? x.href : null;
  } catch { return null; }
}
function sevColor(sev) {
  return getComputedStyle(document.documentElement).getPropertyValue("--sev-" + sev).trim() || "#64748b";
}
function ago(iso) {
  if (!iso) return "";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  const a = Math.abs(s);
  const txt = a < 60 ? `${a}s` : a < 3600 ? `${Math.round(a / 60)}m` : a < 172800 ? `${Math.round(a / 3600)}h` : `${Math.round(a / 86400)}d`;
  return s >= 0 ? `${txt} ago` : `in ${txt}`;
}
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function clock(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function isNewEvent(p) {
  return !p.baseline && Date.now() - new Date(p.first_seen).getTime() < RECENT_MS;
}
function isEscalated(p) {
  return p.prev_severity && p.severity_changed_at && SEV.indexOf(p.severity) > SEV.indexOf(p.prev_severity)
    && Date.now() - new Date(p.severity_changed_at).getTime() < RECENT_MS;
}
function sourceName(id) {
  const s = state.sources.find((x) => x.id === id);
  return s ? s.name : id;
}
function catLabel(id) {
  const c = state.config?.categories.find((x) => x.id === id);
  return c ? c.label : id;
}
function metricLabel(k) {
  const s = k.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
function metricValue(v) {
  if (typeof v === "number") return v.toLocaleString();
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "string" && /^\d{4}-\d\d-\d\dT/.test(v)) return fmtTime(v);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const err = new Error(`${res.status} ${res.statusText}`);
    err.status = res.status;
    try { err.detail = (await res.json()).detail; } catch { /* not json */ }
    throw err;
  }
  return res.json();
}

// --- map -------------------------------------------------------------------------------------------

const map = L.map("map", { worldCopyJump: true }).setView([39, -97], 4);
map.createPane("areas").style.zIndex = 390;
map.createPane("lines").style.zIndex = 395;

const carto = (style) => L.tileLayer(`https://{s}.basemaps.cartocdn.com/${style}/{z}/{x}/{y}{r}.png`, {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
  subdomains: "abcd",
  maxZoom: 19,
});
const baseLayers = {
  Light: carto("light_all"),
  Dark: carto("dark_all"),
  Streets: L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  }),
  Satellite: L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Imagery &copy; Esri",
    maxZoom: 19,
  }),
};
const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
(baseLayers[load("base", prefersDark ? "Dark" : "Light")] || baseLayers.Light).addTo(map);
L.control.layers(baseLayers, null, { position: "topright" }).addTo(map);
L.control.scale({ imperial: true, metric: true }).addTo(map);
map.on("baselayerchange", (e) => save("base", e.name));

// Areas and lines draw directly; every point goes into one cluster group so a national view stays readable.
const shapesLayer = L.layerGroup().addTo(map);
const pointsLayer = L.markerClusterGroup({
  disableClusteringAtZoom: 11,
  maxClusterRadius: 45,
  showCoverageOnHover: false,
  chunkedLoading: true,
  iconCreateFunction(cluster) {
    let max = 0;
    for (const m of cluster.getAllChildMarkers()) max = Math.max(max, m.options.sevRank || 0);
    const n = cluster.getChildCount();
    const size = n < 10 ? 30 : n < 100 ? 36 : 44;
    return L.divIcon({
      className: "",
      html: `<div class="cluster" data-sev="${SEV[max]}" style="width:${size}px;height:${size}px"><span>${n}</span></div>`,
      iconSize: [size, size],
    });
  },
}).addTo(map);
const layerByUid = new Map();

function makeLayer(f) {
  const p = f.properties;
  const g = f.geometry;
  const color = sevColor(p.severity);
  const kind = p.metrics && p.metrics.kind;
  if (g.type === "Point") {
    const [lon, lat] = g.coordinates;
    if (kind === "outage" || kind === "outage_cluster") {
      const d = Math.round(Math.min(44, 10 + Math.sqrt(p.metrics.customers_out || 1) / 2.5));
      const icon = L.divIcon({
        className: "",
        html: `<div class="outage-dot" data-sev="${esc(p.severity)}" style="width:${d}px;height:${d}px"></div>`,
        iconSize: [d, d],
      });
      return L.marker([lat, lon], { icon, sevRank: p.severity_rank }).bindTooltip(esc(p.title));
    }
    const icon = L.divIcon({
      className: "",
      html: `<div class="pin${isNewEvent(p) ? " is-new" : ""}" data-sev="${esc(p.severity)}">${GLYPH[p.category] || GLYPH.other}</div>`,
      iconSize: [28, 28],
      iconAnchor: [14, 14],
      popupAnchor: [0, -12],
    });
    return L.marker([lat, lon], { icon, title: p.title, riseOnHover: true, zIndexOffset: p.severity_rank * 100, sevRank: p.severity_rank });
  }
  const isArea = g.type.includes("Polygon");
  // Context areas (declarations, forecast cones, outlooks) are outlined, not filled, so hazards stay readable.
  const context = CONTEXT_KINDS.has(kind);
  return L.geoJSON(f, {
    pane: isArea ? "areas" : "lines",
    style: !isArea
      ? { color, weight: 5, opacity: 0.85, lineCap: "round" }
      : context
        ? { color, weight: 2, dashArray: "6 5", fillColor: color, fillOpacity: 0.04 }
        : { color, weight: 1.5, fillColor: color, fillOpacity: p.severity_rank >= 3 ? 0.18 : 0.1 },
  }).bindTooltip(esc(p.title), { sticky: true });
}

function popupHtml(p) {
  const rows = [];
  if (p.area && p.area !== sourceName(p.source)) rows.push(["Area", p.area]);
  rows.push(["Source", sourceName(p.source)]);
  if (p.starts_at) rows.push(["Started", fmtTime(p.starts_at)]);
  if (p.updated_at) rows.push(["Updated", `${fmtTime(p.updated_at)} (${ago(p.updated_at)})`]);
  if (p.expires_at) rows.push([p.active ? "Until" : "Ended", fmtTime(p.expires_at)]);
  rows.push(["First seen", fmtTime(p.first_seen) + (p.baseline ? " (at start-up)" : "")]);
  if (p.prev_severity && p.severity_changed_at) {
    rows.push(["Severity", `${p.prev_severity} → ${p.severity} at ${fmtTime(p.severity_changed_at)}`]);
  }
  for (const [k, v] of Object.entries(p.metrics || {})) {
    if (v == null || v === "" || k === "kind" || (Array.isArray(v) && !v.length)) continue;
    if (k === "utility" && v === sourceName(p.source)) continue;
    rows.push([metricLabel(k), metricValue(v)]);
  }
  const link = safeUrl(p.url);
  const resolve = p.source === "field_reports" && p.active
    ? `<button class="btn small" data-resolve="${esc(p.id)}">Mark resolved</button>` : "";
  return `
    <div class="pop-title">${esc(p.title)}</div>
    <div class="pop-chips"><span class="chip" data-sev="${esc(p.severity)}">${esc(p.severity)}</span><span class="chip cat">${esc(catLabel(p.category))}</span></div>
    <table class="kv">${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("")}</table>
    ${p.description ? `<div class="desc">${esc(p.description)}</div>` : ""}
    <div class="pop-actions">${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">Source ↗</a>` : ""}${resolve}</div>`;
}

map.on("popupopen", (e) => {
  const btn = e.popup.getElement().querySelector("[data-resolve]");
  if (btn) btn.addEventListener("click", () => resolveReport(btn.dataset.resolve));
});

// --- filtering & rendering ----------------------------------------------------------------------------

function visible(f) {
  const p = f.properties;
  if (state.hidden.has(p.category)) return false;
  if (p.severity_rank < state.minSev) return false;
  if (state.search) {
    const hay = `${p.title} ${p.area || ""} ${p.description || ""} ${sourceName(p.source)}`.toLowerCase();
    if (!hay.includes(state.search)) return false;
  }
  return true;
}

function renderMap() {
  const reopen = state.openUid;
  shapesLayer.clearLayers();
  pointsLayer.clearLayers();
  layerByUid.clear();
  const points = [];
  const feats = state.features
    .filter((f) => f.geometry && visible(f))
    .sort((a, b) => a.properties.severity_rank - b.properties.severity_rank); // most severe drawn last (on top)
  for (const f of feats) {
    const p = f.properties;
    const layer = makeLayer(f);
    if (!layer) continue;
    layer.bindPopup(() => popupHtml(p), { maxWidth: 360 });
    layer.on("popupopen", () => { state.openUid = p.uid; });
    layer.on("popupclose", () => { if (state.openUid === p.uid) state.openUid = null; });
    layer.on("click", () => { state.selected = p.uid; renderList(); });
    if (layer.getLatLng) points.push(layer); else shapesLayer.addLayer(layer);
    layerByUid.set(p.uid, layer);
  }
  pointsLayer.addLayers(points);
  if (reopen && layerByUid.has(reopen)) openLayerPopup(layerByUid.get(reopen));
}

function openLayerPopup(layer) {
  if (layer.getLatLng) pointsLayer.zoomToShowLayer(layer, () => layer.openPopup());
  else layer.openPopup(layer.getBounds().getCenter());
}

function renderTiles() {
  if (!state.summary) return;
  const html = state.summary.categories
    .filter((c) => c.count > 0 || c.category !== "other")
    .map((c) => {
      const off = state.hidden.has(c.category);
      return `<button class="tile${c.count ? "" : " empty"}${off ? " off" : ""}" data-cat="${esc(c.category)}"
          data-sev="${esc(c.max_severity || "info")}" aria-pressed="${!off}" title="${off ? "Show" : "Hide"} ${esc(c.label)} on map and list">
        <div class="t-head"><span class="t-label">${GLYPH[c.category] || ""} ${esc(c.label)}</span><span class="t-count">${c.count}</span></div>
        <div class="t-headline">${esc(c.headline || "Nothing active")}</div>
        ${c.new_last_hour ? `<div class="t-new">${c.new_last_hour} new in last hour</div>` : ""}
      </button>`;
    })
    .join("");
  $("#tiles").innerHTML = html;
}

function itemHtml(f) {
  const p = f.properties;
  const badges = [];
  if (isNewEvent(p)) badges.push('<span class="badge new">NEW</span>');
  if (isEscalated(p)) badges.push(`<span class="badge up">▲ ${esc(p.prev_severity)}→${esc(p.severity)}</span>`);
  if (!f.geometry) badges.push('<span class="badge nomap">no map location</span>');
  const src = sourceName(p.source);
  const meta = [p.area !== src ? p.area : null, src, ago(p.updated_at || p.first_seen)].filter(Boolean).join(" · ");
  const expanded = state.expanded === p.uid ? `<div class="details">${popupHtml(p)}</div>` : "";
  return `<li><button class="event${state.selected === p.uid ? " selected" : ""}" data-uid="${esc(p.uid)}" data-sev="${esc(p.severity)}">
      <span class="glyph" aria-hidden="true">${GLYPH[p.category] || GLYPH.other}</span>
      <span class="body"><span class="title" style="display:block">${badges.join("")}${esc(p.title)}</span>
      <span class="meta" style="display:block">${esc(meta)}</span></span>
    </button>${expanded}</li>`;
}

function renderList() {
  const feats = state.features.filter(visible);
  $("#events-count").textContent = feats.length;
  const list = $("#event-list");
  if (!feats.length) {
    list.innerHTML = `<li class="empty-state">${state.features.length ? "No events match the current filters." : "No active events."}</li>`;
    return;
  }
  list.innerHTML = feats.slice(0, state.listLimit).map(itemHtml).join("");
  const more = $("#list-more");
  const rest = feats.length - state.listLimit;
  more.hidden = rest <= 0;
  if (rest > 0) more.textContent = `Show ${Math.min(rest, LIST_PAGE)} more (${rest} not shown)`;
}

function renderChanges() {
  const items = state.timeline;
  $("#changes-count").textContent = items.length || "";
  const list = $("#changes-list");
  if (!items.length) {
    list.innerHTML = '<li class="empty-state">No changes in this window. Items loaded at start-up are not counted as new.</li>';
    return;
  }
  list.innerHTML = items.map((i) => `
    <li class="change" data-uid="${esc(i.uid)}" tabindex="0">
      <span class="when">${esc(clock(i.change_at))}</span>
      <span class="kind ${esc(i.change)}">${esc(CHANGE_LABEL[i.change] || i.change)}${i.change === "escalated" || i.change === "deescalated" ? `<span class="sev-change">${esc(i.prev_severity)} → ${esc(i.severity)}</span>` : ""}</span>
      <span><span style="display:block">${GLYPH[i.category] || ""} ${esc(i.title)}</span><span class="muted small">${esc([i.area !== sourceName(i.source) ? i.area : null, sourceName(i.source)].filter(Boolean).join(" · "))}</span></span>
    </li>`).join("");
}

function renderSources() {
  const bad = state.sources.filter((s) => s.health === "failing" || s.health === "stale").length;
  const badge = $("#sources-alert");
  badge.hidden = !bad;
  badge.textContent = bad;
  const order = (s) => {
    const i = CAT_ORDER.indexOf(s.category);
    return i < 0 ? 99 : i;
  };
  const sorted = [...state.sources].sort((a, b) => order(a) - order(b) || a.name.localeCompare(b.name));
  let lastCat = null;
  $("#source-list").innerHTML = sorted.map((s) => {
    const header = s.category !== lastCat ? `<li class="source-group">${GLYPH[s.category] || ""} ${esc(catLabel(s.category))}</li>` : "";
    lastCat = s.category;
    const meta = [];
    if (s.last_success) meta.push(`updated ${ago(s.last_success)}`);
    if (s.event_count != null) meta.push(`${s.event_count} events`);
    if (s.interval) meta.push(`every ${s.interval >= 120 ? Math.round(s.interval / 60) + " min" : s.interval + " s"}`);
    const canRefresh = !["not_configured", "disabled"].includes(s.health) && s.id !== "field_reports";
    const err = s.health === "failing" || s.health === "not_configured"
      ? `<div class="s-error${s.health === "not_configured" ? " info" : ""}">${esc(s.last_error || "")}</div>` : "";
    const stale = s.health === "stale" ? '<div class="s-error">No successful update for over 3 polling intervals.</div>' : "";
    const m = s.meta || {};
    const signup = s.health === "not_configured" && safeUrl(m.signup)
      ? `<div class="s-meta"><a href="${esc(safeUrl(m.signup))}" target="_blank" rel="noopener noreferrer">Get a free key ↗</a></div>` : "";
    const notes = m.notes ? `<div class="s-meta">${esc(m.notes)}</div>` : "";
    const where = s.states && s.states.length ? ` · ${esc(s.states.join(", "))}` : "";
    const conf = m.confidence && m.confidence !== "high"
      ? ` <span class="note" title="How well this feed's endpoint has been verified">${esc(m.confidence)} confidence</span>` : "";
    return `${header}<li class="source">
      <div class="s-head"><span class="health ${esc(s.health)}" title="${esc(s.health.replace("_", " "))}"></span>
        <span class="s-name">${esc(s.name)} ${s.note ? `<span class="note">${esc(s.note)}</span>` : ""}${conf}</span>
        ${canRefresh ? `<button class="btn small" data-refresh="${esc(s.id)}">Refresh</button>` : ""}</div>
      <div class="s-meta">${esc(s.type)}${where}${meta.length ? " · " + esc(meta.join(" · ")) : ""}</div>
      ${notes}${err}${stale}${signup}
    </li>`;
  }).join("");
}

function renderStates() {
  const rows = state.stateRollup;
  const sel = $("#state-filter");
  const current = state.stateFilter;
  const known = rows.filter((r) => r.state !== "??");
  const opts = ['<option value="">All states</option>'].concat(
    known.map((r) => `<option value="${esc(r.state)}">${esc(r.name)} (${r.count})</option>`));
  if (current && !known.some((r) => r.state === current)) opts.push(`<option value="${esc(current)}">${esc(current)} (0)</option>`);
  sel.innerHTML = opts.join("");
  sel.value = current;
  if (!rows.length) {
    $("#state-table").innerHTML = '<tr><td class="empty-state">No active events.</td></tr>';
    return;
  }
  const dot = (sev, n) => (n ? `<span class="sev-count" data-sev="${sev}">${n}</span>` : '<span class="muted">·</span>');
  $("#state-table").innerHTML = `<thead><tr><th>State</th><th title="Extreme">Ext</th><th title="Severe">Sev</th><th title="Moderate">Mod</th><th>All</th><th>Power out</th></tr></thead><tbody>` +
    rows.map((r) => `<tr data-state="${esc(r.state)}" class="${r.state === current ? "selected" : ""}${r.state === "??" ? " unassigned" : ""}" tabindex="0">
      <td>${esc(r.name)}</td><td>${dot("extreme", r.by_severity.extreme)}</td><td>${dot("severe", r.by_severity.severe)}</td>
      <td>${dot("moderate", r.by_severity.moderate)}</td><td>${r.count}</td>
      <td>${r.customers_out ? r.customers_out.toLocaleString() : '<span class="muted">·</span>'}</td></tr>`).join("") + "</tbody>";
}

function setStateFilter(code) {
  state.stateFilter = code || "";
  save("stateFilter", state.stateFilter);
  state.listLimit = LIST_PAGE;
  const row = state.stateRollup.find((r) => r.state === code);
  if (row && row.bbox) map.fitBounds([[row.bbox[1], row.bbox[0]], [row.bbox[3], row.bbox[2]]], { padding: [10, 10] });
  else if (!code) fitArea();
  refresh();
}

function fitArea() {
  const bb = state.config.area.bbox;
  if (bb) map.fitBounds([[bb[1], bb[0]], [bb[3], bb[2]]]);
  else map.setView([37.5, -96], 4);
}

function renderAll() {
  renderStates();
  renderTiles();
  renderMap();
  renderList();
  renderChanges();
  renderSources();
}

function selectEvent(uid, fromList) {
  state.selected = uid;
  const layer = layerByUid.get(uid);
  if (layer && fromList) {
    if (!layer.getLatLng) map.fitBounds(layer.getBounds(), { maxZoom: 11, padding: [30, 30] });
    openLayerPopup(layer);
    state.expanded = null;
  } else if (!layer) {
    state.expanded = state.expanded === uid ? null : uid;
  }
  renderList();
}

// --- data loading ----------------------------------------------------------------------------------

async function refresh() {
  try {
    const hours = $("#changes-hours").value;
    const st = state.stateFilter ? `state=${encodeURIComponent(state.stateFilter)}` : "";
    const [events, summary, sources, timeline, rollup] = await Promise.all([
      api(`/api/events?${st}`),
      api(`/api/summary?${st}`),
      api("/api/sources"),
      api(`/api/timeline?hours=${hours}&${st}`),
      api("/api/states"),
    ]);
    state.features = events.features;
    state.summary = summary;
    state.sources = sources.sources;
    state.timeline = timeline.items;
    state.stateRollup = rollup.states;
    renderAll();
    $("#updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  } catch (err) {
    $("#updated").textContent = `Update failed (${err.message}) — retrying`;
  }
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 1200);
}

function setLive(on) {
  const el = $("#live");
  el.classList.toggle("on", on);
  el.querySelector(".label").textContent = on ? "Live" : "Reconnecting…";
}

function connectStream() {
  const es = new EventSource("/api/stream");
  es.onopen = () => setLive(true);
  es.onerror = () => setLive(false);
  es.addEventListener("update", scheduleRefresh);
}

// --- field reports --------------------------------------------------------------------------------

function writeHeaders() {
  const h = { "Content-Type": "application/json" };
  const token = load("token", null);
  if (token) h["X-EMAgg-Token"] = token;
  return h;
}

async function withToken(fn) {
  try {
    return await fn();
  } catch (err) {
    if (err.status !== 401) throw err;
    const token = window.prompt("This server requires a write token for changes. Enter it:");
    if (!token) throw err;
    save("token", token);
    return fn();
  }
}

function startPlacing() {
  state.placing = true;
  document.body.classList.add("placing");
  $("#place-hint").hidden = false;
}
function stopPlacing() {
  state.placing = false;
  document.body.classList.remove("placing");
  $("#place-hint").hidden = true;
}

map.on("click", (e) => {
  if (!state.placing) return;
  stopPlacing();
  state.pendingLatLng = e.latlng;
  const form = $("#report-form");
  form.reset();
  form.reporter.value = load("reporter", "");
  $("#report-where").textContent = `Location: ${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)}`;
  $("#report-error").hidden = true;
  $("#report-dialog").showModal();
});

$("#report-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const body = {
    category: form.category.value,
    severity: form.severity.value,
    title: form.title.value.trim(),
    description: form.description.value.trim() || null,
    expires_hours: Number(form.expires_hours.value),
    reporter: form.reporter.value.trim() || null,
    lat: state.pendingLatLng.lat,
    lon: state.pendingLatLng.lng,
  };
  save("reporter", body.reporter || "");
  try {
    await withToken(() => api("/api/reports", { method: "POST", headers: writeHeaders(), body: JSON.stringify(body) }));
    $("#report-dialog").close();
    refresh();
  } catch (err) {
    const el = $("#report-error");
    el.textContent = `Could not save: ${err.detail ? JSON.stringify(err.detail) : err.message}`;
    el.hidden = false;
  }
});
$("#report-cancel").addEventListener("click", () => $("#report-dialog").close());

async function resolveReport(id) {
  try {
    await withToken(() => api(`/api/reports/${encodeURIComponent(id)}/resolve`, { method: "POST", headers: writeHeaders() }));
    map.closePopup();
    refresh();
  } catch (err) {
    alert(`Could not resolve: ${err.message}`);
  }
}

// --- UI wiring ------------------------------------------------------------------------------------

$("#report-btn").addEventListener("click", () => (state.placing ? stopPlacing() : startPlacing()));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.placing) stopPlacing(); });

$("#tiles").addEventListener("click", (e) => {
  const tile = e.target.closest(".tile");
  if (!tile) return;
  const cat = tile.dataset.cat;
  if (state.hidden.has(cat)) state.hidden.delete(cat); else state.hidden.add(cat);
  save("hidden", [...state.hidden]);
  renderTiles();
  renderMap();
  renderList();
});

$("#event-list").addEventListener("click", (e) => {
  const resolve = e.target.closest("[data-resolve]");
  if (resolve) return resolveReport(resolve.dataset.resolve);
  const item = e.target.closest(".event");
  if (item) selectEvent(item.dataset.uid, true);
});

$("#changes-list").addEventListener("click", (e) => {
  const item = e.target.closest(".change");
  if (!item) return;
  if (state.features.some((f) => f.properties.uid === item.dataset.uid)) {
    switchTab("events");
    selectEvent(item.dataset.uid, true);
  }
});

$("#source-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-refresh]");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "Queued…";
  try {
    await withToken(() => api(`/api/sources/${encodeURIComponent(btn.dataset.refresh)}/refresh`, { method: "POST", headers: writeHeaders() }));
  } catch (err) {
    btn.textContent = `Failed: ${err.message}`;
  }
  scheduleRefresh();
});

$("#search").addEventListener("input", (e) => {
  state.search = e.target.value.trim().toLowerCase();
  state.listLimit = LIST_PAGE;
  renderMap();
  renderList();
});

const minSevSelect = $("#min-severity");
minSevSelect.value = String(state.minSev);
minSevSelect.addEventListener("change", (e) => {
  state.minSev = Number(e.target.value);
  save("minSev", state.minSev);
  renderMap();
  renderList();
});

$("#changes-hours").addEventListener("change", refresh);
$("#state-filter").addEventListener("change", (e) => setStateFilter(e.target.value));
$("#state-table").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-state]");
  if (!row || row.dataset.state === "??") return;
  setStateFilter(row.dataset.state === state.stateFilter ? "" : row.dataset.state);
  switchTab("events");
});
$("#list-more").addEventListener("click", () => {
  state.listLimit += LIST_PAGE;
  renderList();
});

function switchTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
  });
  for (const id of ["events", "changes", "states", "sources"]) $(`#tab-${id}`).hidden = id !== name;
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

// --- start ----------------------------------------------------------------------------------------

(async function init() {
  state.config = await api("/api/config");
  document.title = state.config.title;
  $("#app-title").textContent = state.config.title;
  $("#area-name").textContent = state.config.area.name;
  $("#demo-banner").hidden = !state.config.demo;
  fitArea();
  await refresh();
  const saved = state.stateRollup.find((r) => r.state === state.stateFilter);
  if (saved && saved.bbox) map.fitBounds([[saved.bbox[1], saved.bbox[0]], [saved.bbox[3], saved.bbox[2]]], { padding: [10, 10] });
  connectStream();
  setInterval(refresh, 60000); // safety net in case the live stream drops silently
  setInterval(() => { renderList(); renderSources(); }, 30000); // keep relative times fresh
})();
