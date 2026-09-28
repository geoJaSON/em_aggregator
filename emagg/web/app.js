"use strict";

const SEV = ["info", "minor", "moderate", "severe", "extreme"];
const GLYPH = {
  weather: "⛈️", flood: "🌊", power: "⚡", comms: "📶", roads: "🚧",
  fire: "🔥", seismic: "〰️", tropical: "🌀", transport: "✈️", shelter: "🏠", other: "📍",
};
const CHANGE_LABEL = { new: "New", escalated: "Escalated", deescalated: "Eased", ended: "Cleared", expired: "Expired" };
const RECENT_MS = 60 * 60 * 1000;
const LIST_PAGE = 300;
// Context areas (declarations, forecast cones, outlooks, state-wide wireline figures) are drawn outlined, behind
// everything else and not clickable on the map, so they never cover the hazards inside them. The list still opens them.
const CONTEXT_KINDS = new Set(["fema_declaration", "nhc_cone", "outlook", "dirs_wireline"]);
// Outage points are always drawn as sized dots in the cluster group, whatever geometry the feed sends.
const OUTAGE_KINDS = new Set(["outage", "outage_cluster"]);
const CAT_ORDER = ["weather", "flood", "tropical", "power", "comms", "roads", "transport", "fire", "seismic", "shelter", "other"];
const EVENTS_LIMIT = 5000; // what the dashboard asks /api/events for; the server says what it left out
const REFRESH_MIN_MS = 15000; // live updates refetch at most this often
const SAFETY_POLL_MS = 60000; // full refetch if nothing arrived for this long (the live stream can drop silently)
const HEALTH_ORDER = ["failing", "stale", "pending", "ok", "not_configured", "disabled"];
const HEALTH_LABEL = { failing: "failing", stale: "stale", pending: "pending", ok: "ok", not_configured: "not configured", disabled: "off" };
const GROUP_OPEN_MAX = 25; // source groups with more feeds than this start collapsed
const KIND_LABEL = {
  outage: ["outage point", "outage points"],
  outage_cluster: ["outage cluster", "outage clusters"],
  county_outage: ["county outage", "county outages"],
  utility_total: ["utility total", "utility totals"],
  fema_declaration: ["FEMA declaration", "FEMA declarations"],
};

const $ = (sel) => document.querySelector(sel);
const state = {
  config: null,
  features: [],
  truncation: null,
  summary: null,
  sources: [],
  sourcesById: new Map(),
  timeline: [],
  stateRollup: [],
  stateFilter: load("stateFilter", ""),
  listLimit: LIST_PAGE,
  hidden: new Set(load("hidden", [])),
  minSev: Number(load("minSev", 0)),
  search: "",
  sourceSearch: "",
  sourceHealth: "",
  sourceGroups: load("sourceGroups", {}),
  selected: null,
  expanded: null,
  openUid: null,
  openLatLng: null,
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
  const s = state.sourcesById.get(id);
  return s ? s.name : id;
}
function catLabel(id) {
  const c = state.config?.categories?.find((x) => x.id === id);
  return c ? c.label : id;
}
function stateName(code) {
  const names = state.config?.state_names || {};
  return names[code] || state.stateRollup.find((r) => r.state === code)?.name || code;
}
function num(n) {
  return Number(n || 0).toLocaleString();
}
function metricLabel(k) {
  const s = k.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
// Same display rule as the server's titles: 12% / 4.2% / <0.1%.
function fmtPercent(v) {
  if (v === 0) return "0%";
  if (v > 0 && v < 0.1) return "<0.1%";
  return `${v >= 10 ? v.toFixed(0) : v.toFixed(1)}%`;
}
function metricValue(v, key = "") {
  if (typeof v === "number" && /^percent/i.test(key)) return fmtPercent(v);
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
const contextPane = map.createPane("context");
contextPane.style.zIndex = 380; // below every hazard area and line
contextPane.style.pointerEvents = "none";
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
let afterPointsLoaded = null; // run once the cluster group has put its (chunk-loaded) markers on the map
const shapesLayer = L.layerGroup().addTo(map);
const pointsLayer = L.markerClusterGroup({
  disableClusteringAtZoom: 11,
  maxClusterRadius: 45,
  showCoverageOnHover: false,
  chunkedLoading: true,
  chunkProgress(done, total) {
    // Markers are placed on the map right after the last chunk is processed.
    if (done === total && afterPointsLoaded) {
      const fn = afterPointsLoaded;
      afterPointsLoaded = null;
      setTimeout(fn, 0);
    }
  },
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

function eventKind(f) {
  return f.properties.metrics ? f.properties.metrics.kind : undefined;
}
function isContext(f) {
  return CONTEXT_KINDS.has(eventKind(f)) && f.geometry && f.geometry.type !== "Point";
}

// Where to put an outage dot: the point itself, else the representative point the store keeps (lat/lon), else
// the middle of the outline (TECO and Kubra send outage outlines).
function outageLatLng(f) {
  const p = f.properties;
  const g = f.geometry;
  if (g.type === "Point") return [g.coordinates[1], g.coordinates[0]];
  if (Number.isFinite(p.lat) && Number.isFinite(p.lon)) return [p.lat, p.lon];
  try {
    const c = L.geoJSON(g).getBounds().getCenter();
    return [c.lat, c.lng];
  } catch {
    return null;
  }
}

function makeLayer(f) {
  const p = f.properties;
  const g = f.geometry;
  const color = sevColor(p.severity);
  const kind = eventKind(f);
  if (OUTAGE_KINDS.has(kind)) {
    const at = outageLatLng(f);
    if (!at) return null;
    const d = Math.round(Math.min(44, 10 + Math.sqrt(p.metrics.customers_out || 1) / 2.5));
    const icon = L.divIcon({
      className: "",
      html: `<div class="outage-dot" data-sev="${esc(p.severity)}" style="width:${d}px;height:${d}px"></div>`,
      iconSize: [d, d],
    });
    return L.marker(at, { icon, sevRank: p.severity_rank }).bindTooltip(esc(p.title));
  }
  if (g.type === "Point") {
    const [lon, lat] = g.coordinates;
    const icon = L.divIcon({
      className: "",
      html: `<div class="pin${isNewEvent(p) ? " is-new" : ""}" data-sev="${esc(p.severity)}">${GLYPH[p.category] || GLYPH.other}</div>`,
      iconSize: [28, 28],
      iconAnchor: [14, 14],
      popupAnchor: [0, -12],
    });
    return L.marker([lat, lon], { icon, title: p.title, riseOnHover: true, zIndexOffset: p.severity_rank * 100, sevRank: p.severity_rank });
  }
  if (isContext(f)) {
    // Outlined, behind everything and not clickable: the hazards inside stay readable and take the clicks.
    return L.geoJSON(f, {
      pane: "context",
      interactive: false,
      style: { color, weight: 2, dashArray: "6 5", fillColor: color, fillOpacity: 0.04 },
    });
  }
  const isArea = g.type.includes("Polygon");
  return L.geoJSON(f, {
    pane: isArea ? "areas" : "lines",
    style: !isArea
      ? { color, weight: 5, opacity: 0.85, lineCap: "round" }
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
    rows.push([metricLabel(k), metricValue(v, k)]);
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
  document.body.classList.add("popup-open"); // lets a small screen hide the legend that would cover it
});
map.on("popupclose", () => document.body.classList.remove("popup-open"));

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
  const reopenAt = state.openLatLng;
  shapesLayer.clearLayers();
  pointsLayer.clearLayers();
  layerByUid.clear();
  afterPointsLoaded = null;
  const points = [];
  const popupOpts = { maxWidth: 360, maxHeight: Math.max(180, map.getSize().y - 70) }; // scroll inside a short (phone) map
  // Context areas first (their own lower pane keeps them behind anyway), then by severity: most severe on top.
  const drawOrder = (f) => (isContext(f) ? -1 : f.properties.severity_rank);
  const feats = state.features
    .filter((f) => f.geometry && visible(f))
    .sort((a, b) => drawOrder(a) - drawOrder(b));
  for (const f of feats) {
    const p = f.properties;
    const layer = makeLayer(f);
    if (!layer) continue;
    layer.bindPopup(() => popupHtml(p), popupOpts);
    layer.on("popupopen", (e) => { state.openUid = p.uid; state.openLatLng = e.popup.getLatLng(); });
    layer.on("popupclose", () => { if (state.openUid === p.uid) state.openUid = null; });
    layer.on("click", () => { state.selected = p.uid; renderList(); });
    if (layer.getLatLng) points.push(layer); else shapesLayer.addLayer(layer);
    layerByUid.set(p.uid, layer);
  }
  if (reopen && layerByUid.has(reopen)) {
    const layer = layerByUid.get(reopen);
    if (layer.getLatLng) afterPointsLoaded = () => reopenPopup(layer); // set before addLayers: it may finish synchronously
    else reopenPopup(layer, reopenAt);
  }
  pointsLayer.addLayers(points);
}

// A user-chosen event: zoom or spiderfy as needed so it can be seen, and pan the popup into view.
function openLayerPopup(layer) {
  if (layer.getLatLng) pointsLayer.zoomToShowLayer(layer, () => layer.openPopup());
  else layer.openPopup(layer.getBounds().getCenter());
}

// After a background refresh: put the popup back where it was without moving the map the user may be panning.
function reopenPopup(layer, at) {
  if (layer.getLatLng && (!layer._map || pointsLayer.getVisibleParent(layer) !== layer)) return; // now inside a cluster
  const popup = layer.getPopup();
  if (!popup) return;
  popup.options.autoPan = false;
  layer.once("popupclose", () => { popup.options.autoPan = true; });
  if (layer.getLatLng) layer.openPopup();
  else layer.openPopup(at || layer.getBounds().getCenter());
}

function renderTiles() {
  if (!state.summary) return;
  const html = state.summary.categories
    .filter((c) => c.count > 0 || c.category !== "other")
    .map((c) => {
      const off = state.hidden.has(c.category);
      const fresh = c.new_last_hour
        ? `<span class="t-new" title="${num(c.new_last_hour)} new in the last hour">+${num(c.new_last_hour)} new</span>` : "";
      return `<button class="tile${c.count ? "" : " empty"}${off ? " off" : ""}" data-cat="${esc(c.category)}"
          data-sev="${esc(c.max_severity || "info")}" aria-pressed="${!off}" title="${off ? "Show" : "Hide"} ${esc(c.label)} on map and list">
        <span class="t-head"><span class="t-label">${GLYPH[c.category] || ""} ${esc(c.label)}</span><span class="t-count">${num(c.count)}</span>${fresh}</span>
        <span class="t-headline">${esc(c.headline || "Nothing active")}</span>
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

function kindLabel(kind, n) {
  const [one, many] = KIND_LABEL[kind] || [String(kind).replace(/_/g, " "), String(kind).replace(/_/g, " ").replace(/([^s])$/, "$1s")];
  return n === 1 ? one : many;
}

function truncationInfo(res, shown) {
  const total = Number.isFinite(res.total) ? res.total : null;
  // Older servers send neither field: a full page is the only hint that something was cut.
  const truncated = res.truncated === true || (total != null && total > shown)
    || (res.truncated == null && total == null && shown >= EVENTS_LIMIT);
  if (!truncated) return null;
  const omitted = res.omitted && typeof res.omitted === "object" ? res.omitted : {};
  return { shown, total, omitted };
}

function truncationText(t) {
  let s = t.total != null ? `Showing ${num(t.shown)} of ${num(t.total)} events` : `Showing the first ${num(t.shown)} events`;
  const parts = Object.entries(t.omitted)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${num(n)} ${kindLabel(k, n)}`);
  if (parts.length) s += ` — ${parts.join(", ")} not shown`;
  else if (t.total != null) s += ` — ${num(t.total - t.shown)} lower-priority events not shown`;
  else s += " — more may exist";
  return s + (state.stateFilter ? "." : ". Pick a state to see more.");
}

function renderTruncation() {
  const t = state.truncation;
  const list = $("#trunc-note");
  const onMap = $("#map-note");
  list.hidden = onMap.hidden = !t;
  if (!t) return;
  const text = truncationText(t);
  list.textContent = text;
  // On a phone the map note is kept to one line; the list note has the details.
  const short = t.total != null ? `${num(t.shown)} of ${num(t.total)} events shown` : `First ${num(t.shown)} events shown`;
  onMap.innerHTML = `<span class="mn-long">${esc(text)}</span><span class="mn-short">${esc(short)}</span>`;
  onMap.title = text;
}

function renderList() {
  const feats = state.features.filter(visible);
  $("#events-count").textContent = num(feats.length);
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
  const pill = $("#changes-count");
  pill.textContent = items.length ? num(items.length) : "";
  pill.hidden = !items.length;
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

const isBadHealth = (s) => s.health === "failing" || s.health === "stale";

// A feed belongs to the selected state when it lists that state, or when it is national (no states).
function sourceInScope(s) {
  const st = state.stateFilter;
  return !st || !Array.isArray(s.states) || !s.states.length || s.states.includes(st);
}

function sourceMatches(s, q) {
  if (!q) return true;
  const m = s.meta || {};
  const hay = [s.name, s.id, s.type, (s.states || []).join(" "), (s.states || []).map(stateName).join(" "), s.last_error,
    m.notes, s.note, catLabel(s.category), HEALTH_LABEL[s.health] || s.health].join(" ").toLowerCase();
  return hay.includes(q);
}

function sourceRowHtml(s, noteCount, printedNotes) {
  const meta = [];
  if (s.last_success) meta.push(`updated ${ago(s.last_success)}`);
  else if (s.last_attempt) meta.push(`tried ${ago(s.last_attempt)}`);
  if (s.event_count != null) meta.push(`${num(s.event_count)} events`);
  if (s.interval) meta.push(`every ${s.interval >= 120 ? Math.round(s.interval / 60) + " min" : s.interval + " s"}`);
  const canRefresh = !["not_configured", "disabled"].includes(s.health) && s.id !== "field_reports";
  const errText = s.last_error || "";
  const err = (s.health === "failing" || s.health === "not_configured") && errText
    ? `<div class="s-error${s.health === "not_configured" ? " info" : ""}" title="${esc(errText)}">${esc(errText)}</div>` : "";
  const stale = s.health === "stale" ? '<div class="s-error">No successful update for over 3 polling intervals.</div>' : "";
  const m = s.meta || {};
  const signup = s.health === "not_configured" && safeUrl(m.signup)
    ? `<div class="s-meta"><a href="${esc(safeUrl(m.signup))}" target="_blank" rel="noopener noreferrer">Get a free key ↗</a></div>` : "";
  // A note shared by many feeds (usually the adapter's description) is printed once; elsewhere it is a tooltip.
  let notes = "";
  let typeTip = "";
  if (m.notes) {
    const shared = (noteCount.get(m.notes) || 0) > 1;
    if (!shared || !printedNotes.has(m.notes)) {
      printedNotes.add(m.notes);
      const prefix = shared ? `<b>Note for ${num(noteCount.get(m.notes))} feeds:</b> ` : "";
      notes = `<div class="s-note" title="${esc(m.notes)}">${prefix}${esc(m.notes)}</div>`;
    } else {
      typeTip = m.notes;
    }
  }
  const where = s.states && s.states.length ? ` · ${esc(s.states.join(", "))}` : " · national";
  const conf = m.confidence && m.confidence !== "high"
    ? ` <span class="note" title="How well this feed's endpoint has been verified">${esc(m.confidence)} confidence</span>` : "";
  const type = typeTip ? `<span class="s-type" title="${esc(typeTip)}">${esc(s.type)} ⓘ</span>` : esc(s.type);
  const health = s.health || "pending";
  return `<li class="source">
    <div class="s-head"><span class="health ${esc(health)}" title="${esc(HEALTH_LABEL[health] || health)}"></span>
      <span class="s-name">${esc(s.name)} ${s.note ? `<span class="note">${esc(s.note)}</span>` : ""}${conf}</span>
      ${canRefresh ? `<button class="btn small" data-refresh="${esc(s.id)}">Refresh</button>` : ""}</div>
    <div class="s-meta">${type}${where}${meta.length ? " · " + esc(meta.join(" · ")) : ""}</div>
    ${notes}${err}${stale}${signup}
  </li>`;
}

function renderSources() {
  const scoped = state.sources.filter(sourceInScope);
  const bad = scoped.filter(isBadHealth).length;
  const badge = $("#sources-alert");
  badge.hidden = !bad;
  badge.textContent = num(bad);
  badge.title = `${num(bad)} failing or stale feed${bad === 1 ? "" : "s"}${state.stateFilter ? ` for ${stateName(state.stateFilter)}` : ""}`;

  const matched = scoped.filter((s) => sourceMatches(s, state.sourceSearch));
  const counts = {};
  for (const s of matched) counts[s.health] = (counts[s.health] || 0) + 1;
  const shown = state.sourceHealth ? matched.filter((s) => s.health === state.sourceHealth) : matched;

  // Summary: scope, health chips (click to filter), and identical errors counted once.
  const scope = state.stateFilter ? `${esc(stateName(state.stateFilter))} and national feeds` : "all feeds";
  const chip = (h, label, n) => `<button type="button" class="hchip" data-health="${esc(h)}" aria-pressed="${state.sourceHealth === h}">`
    + `${h ? `<span class="health ${esc(h)}"></span>` : ""}${num(n)} ${esc(label)}</button>`;
  const healths = HEALTH_ORDER.filter((h) => counts[h] || h === state.sourceHealth)
    .concat(Object.keys(counts).filter((h) => !HEALTH_ORDER.includes(h)));
  const errors = new Map();
  for (const s of shown) if (s.health === "failing" && s.last_error) errors.set(s.last_error, (errors.get(s.last_error) || 0) + 1);
  const common = [...errors].filter(([, n]) => n > 1).sort((a, b) => b[1] - a[1]).slice(0, 4);
  $("#source-summary").innerHTML = `
    <div class="small muted">${shown.length === state.sources.length ? `${num(shown.length)} feeds` : `${num(shown.length)} of ${num(state.sources.length)} feeds`} · ${scope}</div>
    <div class="hchips" role="group" aria-label="Filter feeds by health">${chip("", "all", matched.length)}${healths.map((h) => chip(h, HEALTH_LABEL[h] || h, counts[h] || 0)).join("")}</div>
    ${common.length ? `<ul class="err-summary" aria-label="Most common errors">${common.map(([e, n]) =>
      `<li><button type="button" class="err-sum" data-error="${esc(e)}" title="Show only these feeds"><b>${num(n)} feeds:</b> ${esc(e)}</button></li>`).join("")}</ul>` : ""}`;

  if (!shown.length) {
    $("#source-list").innerHTML = `<li class="empty-state">No feeds match${state.sourceSearch || state.sourceHealth ? " the current filters" : ""}.</li>`;
    return;
  }
  const catOrder = (c) => {
    const i = CAT_ORDER.indexOf(c);
    return i < 0 ? 99 : i;
  };
  const healthOrder = (h) => {
    const i = HEALTH_ORDER.indexOf(h);
    return i < 0 ? HEALTH_ORDER.length : i;
  };
  const sorted = [...shown].sort((a, b) => catOrder(a.category) - catOrder(b.category) || String(a.category).localeCompare(String(b.category))
    || healthOrder(a.health) - healthOrder(b.health) || String(a.name || a.id).localeCompare(String(b.name || b.id)));
  const groups = new Map();
  for (const s of sorted) {
    if (!groups.has(s.category)) groups.set(s.category, []);
    groups.get(s.category).push(s);
  }
  const noteCount = new Map();
  for (const s of state.sources) {
    const n = s.meta && s.meta.notes;
    if (n) noteCount.set(n, (noteCount.get(n) || 0) + 1);
  }
  const printedNotes = new Set();
  const filtering = Boolean(state.sourceSearch || state.sourceHealth);
  const html = [];
  for (const [cat, rows] of groups) {
    const saved = state.sourceGroups[cat];
    const open = typeof saved === "boolean" ? saved : filtering || rows.length <= GROUP_OPEN_MAX;
    const failing = rows.filter(isBadHealth).length;
    html.push(`<li class="source-group"><button type="button" class="group-toggle" data-group="${esc(cat)}" aria-expanded="${open}">
      <span class="caret" aria-hidden="true">${open ? "▾" : "▸"}</span> ${GLYPH[cat] || ""} ${esc(catLabel(cat))}
      <span class="g-counts">${num(rows.length)}${failing ? ` · <span class="g-bad">${num(failing)} failing/stale</span>` : ""}</span></button></li>`);
    if (open) for (const s of rows) html.push(sourceRowHtml(s, noteCount, printedNotes));
  }
  $("#source-list").innerHTML = html.join("");
}

// Every state (or, for a regional area, the area's states plus any other state with events), alphabetically,
// with its active-event count, so a quiet state can be picked to confirm it is clear.
function stateOptionsHtml() {
  const names = state.config?.state_names || {};
  const rollup = new Map(state.stateRollup.filter((r) => r.state !== "??").map((r) => [r.state, r]));
  const areaStates = state.config?.area?.states || [];
  let codes = Object.keys(names);
  if (areaStates.length) codes = codes.filter((c) => areaStates.includes(c));
  const all = new Set(codes);
  for (const c of rollup.keys()) all.add(c);
  for (const c of areaStates) all.add(c);
  if (state.stateFilter) all.add(state.stateFilter);
  const label = (c) => names[c] || rollup.get(c)?.name || c;
  return ['<option value="">All states</option>'].concat([...all]
    .sort((a, b) => label(a).localeCompare(label(b)))
    .map((c) => {
      const r = rollup.get(c);
      return `<option value="${esc(c)}">${esc(label(c))}${r ? ` (${num(r.count)})` : ""}</option>`;
    })).join("");
}

let stateOptionsShown = "";
function renderStates() {
  const rows = state.stateRollup;
  const sel = $("#state-filter");
  const current = state.stateFilter;
  const opts = stateOptionsHtml();
  if (opts !== stateOptionsShown) { // don't rebuild (and close) the list while someone has it open
    sel.innerHTML = opts;
    stateOptionsShown = opts;
  }
  sel.value = current;
  if (!rows.length) {
    $("#state-table").innerHTML = '<tr><td class="empty-state">No active events.</td></tr>';
    return;
  }
  const dot = (sev, n) => (n ? `<span class="sev-count" data-sev="${sev}">${n}</span>` : '<span class="muted">·</span>');
  $("#state-table").innerHTML = `<thead><tr><th>State</th><th title="Extreme">Ext</th><th title="Severe">Sev</th><th title="Moderate">Mod</th><th>All</th><th>Power out</th></tr></thead><tbody>` +
    rows.map((r) => `<tr data-state="${esc(r.state)}" class="${r.state === current ? "selected" : ""}${r.state === "??" ? " unassigned" : ""}" tabindex="0">
      <td>${esc(r.name)}</td><td>${dot("extreme", r.by_severity.extreme)}</td><td>${dot("severe", r.by_severity.severe)}</td>
      <td>${dot("moderate", r.by_severity.moderate)}</td><td>${num(r.count)}</td>
      <td>${r.customers_out ? r.customers_out.toLocaleString() : '<span class="muted">·</span>'}</td></tr>`).join("") + "</tbody>";
}

function setStateFilter(code) {
  state.stateFilter = code || "";
  save("stateFilter", state.stateFilter);
  state.listLimit = LIST_PAGE;
  if (code) fitState(code);
  else fitArea();
  refresh();
}

function stateBbox(code) {
  const row = state.stateRollup.find((r) => r.state === code);
  return (row && row.bbox) || state.config?.state_bboxes?.[code] || null;
}

function fitState(code) {
  const bb = stateBbox(code);
  if (bb) map.fitBounds([[bb[1], bb[0]], [bb[3], bb[2]]], { padding: [10, 10] });
}

function fitArea() {
  const bb = state.config.area.bbox;
  if (bb) map.fitBounds([[bb[1], bb[0]], [bb[3], bb[2]]]);
  else map.setView([37.5, -96], 4);
}

function renderAll() {
  renderStates();
  renderTiles();
  renderTruncation();
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

function setSources(list) {
  state.sources = Array.isArray(list) ? list : [];
  state.sourcesById = new Map(state.sources.map((s) => [s.id, s]));
}

let refreshSeq = 0; // only the newest refresh may render (a slow older one must not overwrite it)
let lastFetchAt = 0; // any refetch, for the live-update throttle
let lastFullAt = 0; // full refetch, for the safety poll

async function refresh() {
  const seq = ++refreshSeq;
  lastFetchAt = lastFullAt = Date.now();
  pending.full = pending.sources = false; // this refetch covers whatever was waiting
  try {
    const hours = $("#changes-hours").value;
    const st = state.stateFilter ? `state=${encodeURIComponent(state.stateFilter)}` : "";
    const [events, summary, sources, timeline, rollup] = await Promise.all([
      api(`/api/events?limit=${EVENTS_LIMIT}&${st}`),
      api(`/api/summary?${st}`),
      api("/api/sources"),
      api(`/api/timeline?hours=${hours}&${st}`),
      api("/api/states"),
    ]);
    if (seq !== refreshSeq) return;
    state.features = Array.isArray(events.features) ? events.features : [];
    state.truncation = truncationInfo(events, state.features.length);
    state.summary = summary;
    setSources(sources.sources);
    state.timeline = timeline.items || [];
    state.stateRollup = rollup.states || [];
    renderAll();
    $("#updated").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  } catch (err) {
    if (seq === refreshSeq) $("#updated").textContent = `Update failed (${err.message}) — retrying`;
  }
}

async function refreshSources() {
  lastFetchAt = Date.now();
  pending.sources = false;
  const seq = refreshSeq;
  try {
    const res = await api("/api/sources");
    if (seq !== refreshSeq) return; // a full refresh started meanwhile and brings its own
    setSources(res.sources);
    renderSources();
  } catch { /* the next refresh retries */ }
}

// Live updates: the server announces only polls that changed something. Refetch at most once per REFRESH_MIN_MS
// (right away if the last fetch was long enough ago, else once at the end of the window), and only the sources
// when a message is just a feed's health changing.
const pending = { full: false, sources: false };
let throttleTimer = null;

function scheduleRefresh(full = true) {
  if (full) pending.full = true;
  else pending.sources = true;
  if (throttleTimer) return;
  const wait = lastFetchAt + REFRESH_MIN_MS - Date.now();
  if (wait <= 0) runPendingRefresh();
  else throttleTimer = setTimeout(runPendingRefresh, wait);
}

function runPendingRefresh() {
  throttleTimer = null;
  if (pending.full) refresh();
  else if (pending.sources) refreshSources();
}

function messageNeedsFullRefresh(data) {
  let msg;
  try { msg = JSON.parse(data); } catch { return true; }
  if (!msg || typeof msg !== "object") return true;
  if (["new", "updated", "ended", "escalated"].some((k) => Number(msg[k]) > 0)) return true;
  // A message without counts is from an unknown sender or an older server: refetch everything to be safe.
  const hasCounts = ["new", "updated", "ended"].some((k) => k in msg);
  return !hasCounts && msg.ok !== false;
}

function setLive(on) {
  const el = $("#live");
  el.classList.toggle("on", on);
  el.querySelector(".label").textContent = on ? "Live" : "Reconnecting…";
}

function connectStream() {
  const es = new EventSource("/api/stream");
  let dropped = false;
  es.onopen = () => {
    setLive(true);
    if (dropped) scheduleRefresh(true); // messages sent while disconnected are lost
    dropped = false;
  };
  es.onerror = () => {
    setLive(false);
    dropped = true;
  };
  es.addEventListener("update", (e) => scheduleRefresh(messageNeedsFullRefresh(e.data)));
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
  const toggle = e.target.closest("[data-group]");
  if (toggle) {
    state.sourceGroups[toggle.dataset.group] = toggle.getAttribute("aria-expanded") !== "true";
    save("sourceGroups", state.sourceGroups);
    renderSources();
    return;
  }
  const btn = e.target.closest("[data-refresh]");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "Queued…";
  try {
    await withToken(() => api(`/api/sources/${encodeURIComponent(btn.dataset.refresh)}/refresh`, { method: "POST", headers: writeHeaders() }));
  } catch (err) {
    btn.textContent = `Failed: ${err.message}`;
  }
  setTimeout(refreshSources, 5000); // a poll that changes nothing sends no live update
});

$("#source-summary").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-health]");
  if (chip) {
    const h = chip.dataset.health;
    state.sourceHealth = state.sourceHealth === h ? "" : h;
    renderSources();
    return;
  }
  const err = e.target.closest("[data-error]");
  if (err) {
    const input = $("#source-search");
    input.value = err.dataset.error;
    state.sourceSearch = err.dataset.error.toLowerCase();
    renderSources();
  }
});

$("#source-search").addEventListener("input", (e) => {
  state.sourceSearch = e.target.value.trim().toLowerCase();
  renderSources();
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
  if (state.stateFilter) fitState(state.stateFilter);
  connectStream();
  // Safety net in case the live stream drops silently: a full refetch once nothing has for SAFETY_POLL_MS.
  setInterval(() => { if (Date.now() - lastFullAt >= SAFETY_POLL_MS) refresh(); }, 5000);
  setInterval(() => { renderList(); renderSources(); }, 30000); // keep relative times fresh
})();
