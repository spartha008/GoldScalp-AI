// Dashboard: current market data, candles + structure overlays, analysis, account overview,
// optional TradingView panel and CSV import.
import { api, latestOnly } from "./api.js";
import { createCandleChart } from "./chart.js";
import { bindAnnotationControls } from "./annotations.js";
import {
  renderLiquidity, renderMTF, renderPerformanceSummary, renderPriceAction, renderSessions, renderSignal, renderStructure,
  renderVolatility, signalBadge,
} from "./intel-panels.js";
import { subscribeQuotes } from "./market-stream.js";
import { initTicket, ticketQuote } from "./ticket.js";
import { initTradingView } from "./tradingview.js";
import {
  announce, clear, el, fmtAge, fmtMoney, fmtPrice, fmtTime, kindBadge, num, pnlClass, setDisplayZone, setText,
  stateBadge, storageGet, storageSet,
} from "./ui.js";

const TF_KEY = "goldscalp:tf:v1";
const state = { tf: storageGet(TF_KEY, "M5"), series: null, chart: null, firstLoad: true, lastPrice: null, analysis: null,
                snapshot: null, signalDirection: null, annot: null, annotControls: null };
if (!["M1", "M5", "M15", "H1", "MN1", "Y1"].includes(state.tf)) state.tf = "M5";
// 1M / 1Y are chart-only calendar timeframes (built from H1 on the server); analysis panels stay on H1 meanwhile
const CALENDAR = new Set(["MN1", "Y1"]);
const analysisTf = () => (CALENDAR.has(state.tf) ? "H1" : state.tf);

// ------------------------------------------------------------------ market panel

function renderQuote(envelope) {
  const snap = envelope && envelope.data;
  if (!snap) return;
  state.snapshot = snap;
  ticketQuote(snap);                                  // MT5 demo order ticket: live estimates and position P/L
  const q = snap.quote;
  document.getElementById("mkt-state").replaceChildren(stateBadge(snap.state));
  document.getElementById("top-state").replaceChildren(stateBadge(snap.state));
  setText("mkt-reason", snap.reason);
  const provider = snap.provider;
  const details = snap.provider_details || {};
  renderProviders(snap.providers);
  renderSourceChange(snap.latest_source_change);
  if (provider) {
    const cadence = provider.poll_seconds ? `, every ${provider.poll_seconds} s` : "";
    setText("mkt-source", `${provider.name} (${provider.transport}${cadence})`);
    setText("mkt-pricekind", `${provider.price_kind}; candles: ${provider.candle_side} prices`);
    setText("mkt-attrib", [provider.attribution, provider.terms_note].filter(Boolean).join(". "));
    setText("foot-attrib", provider.attribution || "");
    setText("mkt-brokersym", details.symbol ? `${details.symbol} (${details.symbol_source || "auto"})` : provider.source_symbol);
    setText("mkt-symbol", details.symbol || provider.source_symbol || "");
  } else {
    setText("mkt-source", "none (stored history only)");
    for (const id of ["mkt-pricekind", "mkt-brokersym", "mkt-symbol"]) setText(id, null);
  }
  const conn = snap.connection || {};
  const parts = [];
  if (conn.link_down_reason) parts.push("link DOWN");
  else if (conn.last_success_at) parts.push("receiving data");
  if (conn.stream_connected) parts.push("stream connected");
  if (details.terminal && details.terminal.connected !== undefined) {
    parts.push(details.terminal.connected ? "broker connected" : "broker NOT connected");
  }
  if (conn.consecutive_failures) parts.push(`${conn.consecutive_failures} failure(s)`);
  setText("mkt-conn", parts.join(" · ") || (conn.enabled ? "starting" : "no provider"));
  setText("mkt-lastok", conn.last_success_at ? fmtTime(conn.last_success_at, true) : "never");
  setText("mkt-reconnects", `${conn.reconnect_attempts || 0} attempt(s), ${conn.reconnects || 0} recovered`);
  setText("mkt-tf", state.tf);
  const errText = conn.link_down_reason || (conn.consecutive_failures ? conn.last_error_message : null);
  announce(document.getElementById("mkt-error"), errText ? `Connection problem: ${errText}` : "", "error");
  const hours = snap.market_hours || {};
  setText("mkt-hours", hours.open ? `open · closes ${fmtTime(hours.next_change, true)}`
    : `closed (${(hours.reason || "").replace("_", " ")}) · opens ${fmtTime(hours.next_change, true)}`);
  renderMT5(provider, details);
  renderFreshness(snap);
  const priceEl = document.getElementById("mkt-price");
  if (!q) {
    for (const id of ["mkt-price", "mkt-bid", "mkt-ask", "mkt-spread", "mkt-time", "mkt-age"]) setText(id, null);
    setText("mkt-price-label", "no price received");
    setText("top-price", null);
    return;
  }
  const price = q.mid ?? q.last;
  const prev = state.lastPrice;
  priceEl.textContent = fmtPrice(price, 2);
  priceEl.classList.toggle("up", prev !== null && num(price) > prev);
  priceEl.classList.toggle("down", prev !== null && num(price) < prev);
  priceEl.classList.toggle("stale", snap.state === "DISCONNECTED");
  state.lastPrice = num(price);
  setText("top-price", fmtPrice(price, 2));
  let staleNote = "";
  if (snap.state === "DISCONNECTED") staleNote = " — last known price, STALE";
  else if (snap.state !== "LIVE") staleNote = ` — ${snap.state.toLowerCase()}`;
  setText("mkt-price-label", (q.mid ? "mid price (bid/ask from provider)" : "last indicative price (provider gives no bid/ask)") + staleNote);
  const digits = details.symbol_spec ? Math.min(details.symbol_spec.digits, 3) : 2;
  setText("mkt-bid", q.bid ? fmtPrice(q.bid, digits) : "not provided");
  setText("mkt-ask", q.ask ? fmtPrice(q.ask, digits) : "not provided");
  const bar = document.getElementById("mkt-spread-bar");
  if (q.spread) {
    setText("mkt-spread", `${fmtPrice(q.spread, digits)} USD`);
    bar.style.width = `${Math.min(100, (num(q.spread) / 1.0) * 100)}%`;
    setText("mkt-spread-note", "Provider bid/ask spread (bar full at $1.00).");
  } else {
    setText("mkt-spread", "not provided by source");
    bar.style.width = "0%";
    setText("mkt-spread-note", "The simulator uses the configured spread instead (shown on the Paper Trading page).");
  }
  setText("mkt-time", fmtTime(q.quote_time, true));
  setText("mkt-age", fmtAge(q.age_seconds));
}

function renderFreshness(snap) {
  const q = snap.quote;
  const bar = document.getElementById("fresh-bar");
  const limit = num(snap.stale_after_seconds) || 15;
  if (!q) { bar.style.width = "0%"; setText("fresh-text", "no data yet"); return; }
  const age = Math.max(0, num(q.age_seconds) || 0);
  bar.style.width = `${Math.max(4, 100 - Math.min(100, (age / limit) * 100))}%`;
  bar.className = snap.state === "DISCONNECTED" ? "down" : age > limit ? "stale" : "";
  setText("fresh-text", `price age ${fmtAge(age)} · LIVE threshold ${limit} s`);
}

function renderProviders(view) {
  const select = document.getElementById("provider-select");
  if (!view || !view.options) return;
  const signature = JSON.stringify(view.options) + view.active;
  if (select.dataset.sig !== signature && document.activeElement !== select) {
    select.dataset.sig = signature;
    clear(select);
    for (const o of view.options) {
      select.append(el("option", { value: o.id, disabled: !o.available, selected: o.id === view.active,
                                   text: o.available ? o.name : `${o.name} (unavailable: ${o.reason})` }));
    }
  }
  const mode = view.fallback_mode === "auto" ? `auto after ${view.fallback_after_seconds} s` : "manual (never switches by itself)";
  setText("provider-note", `Priority: ${view.priority.join(" > ")} > stored history · fallback: ${mode}`);
}

function renderSourceChange(change) {
  const box = document.getElementById("src-change");
  if (!change) { announce(box, ""); return; }
  announce(box, `Data source changed at ${fmtTime(change.at, true)}: ${change.from_name || "none"} → `
    + `${change.to_name}. ${change.reason}.`, "warn");
}

function renderMT5(provider, details) {
  const box = document.getElementById("mt5-box");
  const isMT5 = Boolean(provider && provider.id === "mt5");
  box.hidden = !isMT5;
  if (!isMT5) return;
  const t = details.terminal || {};
  const build = t.build ? ` (build ${t.build})` : "";
  const initError = details.last_init_error ? ` (${details.last_init_error})` : "";
  setText("mt5-terminal", details.initialized ? `connected to terminal${build}` : `not connected${initError}`);
  setText("mt5-broker", t.connected === undefined ? null : `${t.connected ? "connected" : "NOT connected"}${t.broker ? ` · ${t.broker}` : ""}`);
  const spec = details.symbol_spec;
  setText("mt5-symbol", details.symbol ? `${details.symbol} (${details.symbol_source})${spec ? ` · ${spec.description}` : ""}` : "not resolved");
  setText("mt5-contract", spec ? `${spec.contract_size} units per lot · lots ${spec.volume_min}-${spec.volume_max} step ${spec.volume_step} · ${spec.digits} digits` : null);
  let basis = "not yet determined";
  if (details.time_basis) {
    const off = details.server_offset_hours;
    const offText = off === null || off === undefined ? "" : `, server UTC${off >= 0 ? "+" : ""}${off}`;
    basis = `${details.time_basis} (${details.time_basis_source}${offText})`;
  }
  setText("mt5-basis", basis);
  const select = document.getElementById("mt5-symbol-select");
  const candidates = details.candidates || [];
  const signature = candidates.map((c) => c.name).join(",") + "|" + details.symbol;
  if (select.dataset.sig !== signature && document.activeElement !== select) {
    select.dataset.sig = signature;
    clear(select);
    for (const c of candidates) {
      select.append(el("option", { value: c.name, selected: c.name === details.symbol, text: `${c.name} — ${c.description || ""}` }));
    }
  }
}

async function chooseProvider(id, symbol) {
  const out = document.getElementById("mkt-error");
  try {
    await api("/api/market/provider", { method: "POST", body: symbol ? { provider: id, symbol } : { provider: id } });
    state.firstLoad = true;
    state.series = null;
    document.getElementById("series-select").value = "";
    setTimeout(() => { refreshSeriesList(); refreshCandles(); refreshAnalysis(); }, 2500);
  } catch (err) {
    announce(out, `Could not switch provider: ${err.message}`, "error");
  }
}

function initProviderControls() {
  document.getElementById("provider-select").addEventListener("change", (e) => chooseProvider(e.target.value));
  document.getElementById("reconnect-btn").addEventListener("click", async () => {
    try {
      await api("/api/market/reconnect", { method: "POST" });
      setText("mkt-conn", "reconnecting…");
    } catch (err) {
      announce(document.getElementById("mkt-error"), `Reconnect failed: ${err.message}`, "error");
    }
  });
  document.getElementById("mt5-symbol-apply").addEventListener("click", () => {
    const symbol = document.getElementById("mt5-symbol-select").value;
    if (symbol) chooseProvider("mt5", symbol);
  });
}

// ------------------------------------------------------------------ candles

// The chart shows the latest few hundred candles (refreshed every 15 s) plus any older pages loaded on demand:
// scrolling to the left edge (or the button) fetches the PAGE candles before the oldest one shown, back through
// all stored history. The view stays on the candles being looked at.
const BASE_LIMIT = (tf) => (tf === "M1" ? 600 : 500);
const PAGE = 2000;
const history = { older: [], hasMore: true, loading: false, first: null, armed: false, key: null };   // armed: the user moved the chart

const loadCandles = latestOnly(async (tf, series, limit) => {
  const qs = new URLSearchParams({ tf, limit: String(limit) });
  if (series) qs.set("series", series);
  return api(`/api/market/candles?${qs}`);
});

async function loadOlder() {
  if (history.loading || !history.hasMore || history.first === null) return;
  history.loading = true;
  const key = history.key;
  setText("chart-history", "Loading older candles…");
  try {
    const qs = new URLSearchParams({ tf: state.tf, limit: String(PAGE), before: String(history.first) });
    if (state.series) qs.set("series", state.series);
    const page = (await api(`/api/market/candles?${qs}`)).data.candles || [];
    if (key !== history.key) return;                         // timeframe or series changed meanwhile
    history.older = [...page.filter((c) => c.time < history.first), ...history.older];
    if (page.length < PAGE) history.hasMore = false;          // reached the oldest stored candle
    await refreshCandles();
  } catch (err) {
    setText("chart-history", `Could not load older candles: ${err.message}`);
  } finally { history.loading = false; }
}

function onVisibleRange(range) {
  if (range && history.armed && range.from <= 15) loadOlder();
}

async function refreshCandles() {
  if (state.firstLoad || history.key === null) {
    Object.assign(history, { older: [], hasMore: true, first: null, armed: false, key: `${state.tf}|${state.series || ""}|${Date.now()}` });
  }
  const limit = BASE_LIMIT(state.tf);
  let env;
  try { env = await loadCandles(state.tf, state.series, limit); } catch (err) { setText("chart-empty", `Could not load candles: ${err.message}`); return; }
  if (!env) return;  // a newer request superseded this one
  const data = env.data;
  if (data.candles && data.candles.length && history.older.length) {   // older pages + the latest window
    const start = data.candles[0].time;
    data.candles = [...history.older.filter((c) => c.time < start), ...data.candles];
  }
  if (data.candles && data.candles.length < limit) history.hasMore = false;
  const empty = document.getElementById("chart-empty");
  document.getElementById("chart-kind").replaceChildren(kindBadge(env.kind));
  if (!data.series || !data.candles.length) {
    empty.hidden = false;
    empty.textContent = data.message || "No candles yet for this timeframe. Live candles build up while the market is open; you can also import a historical CSV.";
    if (state.chart) state.chart.setCandles([]);
    setText("chart-series", data.series ? data.series.description : "no data series yet");
    return;
  }
  empty.hidden = true;
  if (!state.chart) {
    const box = document.getElementById("chart");
    state.chart = createCandleChart(box);
    for (const ev of ["wheel", "pointerdown", "touchstart", "keydown"]) box.addEventListener(ev, () => { history.armed = true; }, { passive: true });
    state.chart.chart.timeScale().subscribeVisibleLogicalRangeChange(onVisibleRange);
  }
  const ts = state.chart.chart.timeScale();
  const before = state.firstLoad ? null : ts.getVisibleLogicalRange();
  const first = data.candles[0].time;
  // older bars prepended: shift the view by exactly that many bars, so position and zoom stay the same
  const added = history.first !== null && first < history.first ? data.candles.findIndex((c) => c.time === history.first) : 0;
  state.chart.setCandles(data.candles, { fit: state.firstLoad });
  if (before && added > 0) ts.setVisibleLogicalRange({ from: before.from + added, to: before.to + added });
  history.first = first;
  if (CALENDAR.has(state.tf)) {
    history.hasMore = false;
    const partial = data.candles.some((c) => c.partial);
    setText("chart-history", `${data.candles.length} ${state.tf === "MN1" ? "monthly" : "yearly"} candle(s) from stored H1 history`
      + ` (since ${fmtTime(data.source_first, true)})${partial ? " · first one partial (faded)" : ""}`
      + `${data.candles.some((c) => !c.closed) ? " · latest still forming" : ""} · calendar periods in UTC`);
  } else {
    setText("chart-history", `${data.candles.length.toLocaleString("en-US")} ${state.tf} candles loaded`
      + (history.hasMore ? " · scroll back in time to load older ones" : ` · all stored history shown (from ${fmtTime(first, true)})`));
  }
  document.getElementById("chart-older").hidden = !history.hasMore;
  state.firstLoad = false;
  const derived = data.candles.filter((c) => c.origin === "derived_from_M1").length;
  setText("chart-series", `${data.series.key} · ${data.series.description}${derived ? ` · ${derived} bar(s) built from M1` : ""}`);
  const g = data.gaps;
  document.getElementById("chart-gaps").textContent = g ? `Missing candles: ${g.missing} of ${g.expected} expected${g.missing ? ` (latest gap from ${fmtTime(g.missing_ranges.at(-1)?.from, true)})` : ""}` : "";
  renderTwin(data.candles);
  applyOverlays();
}

function renderTwin(candles) {
  const body = clear(document.getElementById("twin-body"));
  for (const c of candles.slice(-10).reverse()) {
    body.append(el("tr", {}, [el("td", { text: fmtTime(c.time, true) }),
      el("td", { class: "num", text: fmtPrice(c.open) }), el("td", { class: "num", text: fmtPrice(c.high) }),
      el("td", { class: "num", text: fmtPrice(c.low) }), el("td", { class: "num", text: fmtPrice(c.close) }),
      el("td", { text: c.closed ? "closed" : "forming" })]));
  }
}

async function refreshSeriesList() {
  const select = document.getElementById("series-select");
  try {
    const env = await api("/api/market/series");
    const current = state.series;
    clear(select);
    const active = env.data.active ? `: ${env.data.active}` : "";
    select.append(el("option", { value: "", text: `Automatic (active provider${active})` }));
    for (const s of env.data.series) {
      const bars = Object.values(s.timeframes || {}).reduce((n, t) => n + t.bars, 0);
      select.append(el("option", { value: s.key, text: `${s.kind === "live" ? "Live" : "History"}: ${s.key} (${bars} bars)` }));
    }
    select.value = current || "";
  } catch (_) { /* keep the old list */ }
}

// ------------------------------------------------------------------ analysis (Phase 2 intelligence)

const loadIntelligence = latestOnly(async (tf, series, sensitivity) => {
  const qs = new URLSearchParams({ tf });
  if (series) qs.set("series", series);
  if (sensitivity) qs.set("swing_strength", sensitivity);
  return api(`/api/analysis/intelligence?${qs}`, { timeoutMs: 30000 });
});

async function refreshAnalysis() {
  let env;
  try { env = await loadIntelligence(analysisTf(), state.series, state.annot ? state.annot.sensitivity : ""); } catch (err) {
    setText("mtf-asof", `analysis unavailable: ${err.message}`);
    return;
  }
  if (!env) return;
  const d = env.data;
  document.getElementById("an-kind").replaceChildren(kindBadge(env.kind));
  document.getElementById("mtf-kind").replaceChildren(kindBadge(env.kind));
  if (!d || !d.signal) { setText("mtf-asof", d && d.message ? d.message : "no analysis"); return; }
  state.analysis = d.overlays;
  applyOverlays();
  setText("mtf-asof", `as of ${fmtTime(d.as_of, true)}`);
  renderMTF(document.getElementById("mtf-body"), d.multi_timeframe);
  const previous = state.signalDirection;
  renderSignal(document.getElementById("sig-body"), d.signal);
  state.signalDirection = d.signal.direction;
  if (previous && previous !== d.signal.direction) {
    setText("sig-live", `Signal centre changed from ${previous} to ${d.signal.direction}.`);
  }
  for (const id of ["liq-tf", "ms-tf", "pa-tf"]) setText(id, d.chart_tf);
  renderStructure(document.getElementById("ms-body"), d.overlays);
  renderPriceAction(document.getElementById("pa-body"), d.overlays);
  renderLiquidity(document.getElementById("liq-body"), d.overlays ? d.overlays.liquidity : null,
                  d.overlays ? d.overlays.last_close : null);
  renderSessions(document.getElementById("ss-body"), d.sessions);
  renderVolatility(document.getElementById("vol-body"), d.volatility);
  refreshSignalHistory(d.series);
}

async function refreshSignalHistory(series) {
  const box = document.getElementById("sig-history");
  try {
    const out = await api(`/api/signals?mode=live&limit=10&series=${encodeURIComponent(series)}`);
    clear(box);
    if (!out.signals.length) { box.append(el("p", { class: "muted small", text: "No stored signals yet." })); return; }
    box.append(el("ul", { class: "evidence" }, out.signals.map((s) => el("li", { class: "small" }, [
      signalBadge(s.direction), el("span", { class: "num", text: ` ${s.score > 0 ? "+" : ""}${s.score} ` }),
      el("span", { class: "muted", text: `cut-off ${fmtTime(s.cutoff, true)}` }),
    ]))));
    box.append(el("p", { class: "muted small", text: out.note }));
  } catch (_) { /* optional */ }
}

function applyOverlays() {
  if (!state.chart || !state.annot) return;
  if (CALENDAR.has(state.tf)) {           // hourly swings and zones do not map onto monthly / yearly bars
    state.chart.clearOverlays();
    setText("chart-density", "Annotations are off on 1M and 1Y; the analysis panels below use H1.");
    return;
  }
  state.chart.setIntelligence(state.analysis, state.annot, {
    max: state.annot.max, onDensity: (text) => setText("chart-density", text),
  });
}

async function refreshPerformance() {
  try {
    const list = await api("/api/performance/runs");
    const done = list.runs.find((r) => r.status === "complete");
    if (!done) { renderPerformanceSummary(document.getElementById("perf-body"), null); return; }
    const out = await api(`/api/performance/runs/${encodeURIComponent(done.id)}`);
    renderPerformanceSummary(document.getElementById("perf-body"), out.run);
  } catch (_) { /* optional */ }
}

// ------------------------------------------------------------------ account overview

async function refreshAccount() {
  try {
    const env = await api("/api/sim/account");
    const s = env.data.summary;
    setText("acct-balance", fmtMoney(s.balance));
    setText("acct-equity", s.equity !== null ? fmtMoney(s.equity) : "unknown (no price)");
    setText("acct-margin", fmtMoney(s.used_margin));
    setText("acct-free", s.free_margin !== null ? fmtMoney(s.free_margin) : "unknown");
    const upl = document.getElementById("acct-upl");
    upl.textContent = s.unrealized_pnl !== null ? fmtMoney(s.unrealized_pnl, { sign: true }) : "unknown";
    upl.className = `value num ${pnlClass(s.unrealized_pnl) ? `trend-${pnlClass(s.unrealized_pnl) === "up" ? "up" : "down"}` : ""}`;
    setText("acct-open", s.open_positions);
    setText("acct-note", `${s.label}.${s.mark_note ? " " + s.mark_note + "." : ""}`);
  } catch (_) { /* leave the last values */ }
}

// ------------------------------------------------------------------ import

function initImport() {
  document.getElementById("import-kind").replaceWith(kindBadge("historical"));
  const form = document.getElementById("import-form");
  const out = document.getElementById("import-result");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    if (!data.get("point")) data.delete("point");
    announce(out, "Importing…");
    try {
      const env = await api("/api/data/import", { method: "POST", form: data, timeoutMs: 120000 });
      const r = env.data;
      announce(out, r.already_imported ? `This file was already imported into ${r.series_key}; nothing changed.`
        : `Imported into ${r.series_key}: ${r.rows_stored} of ${r.rows_read} rows stored, ${r.rows_rejected} rejected, `
          + `${r.duplicates_dropped} duplicate(s) dropped, ${r.gaps.missing} missing candle(s) detected.`, "ok");
      await refreshSeriesList();
      state.series = r.series_key; document.getElementById("series-select").value = r.series_key;
      state.firstLoad = true; refreshCandles(); refreshAnalysis();
    } catch (err) {
      announce(out, `Import refused: ${err.message}`, "error");
    }
  });
}

// ------------------------------------------------------------------ wiring

function initControls() {
  const buttons = [...document.querySelectorAll("#tf-group button")];
  const press = () => buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tf === state.tf)));
  press();
  buttons.forEach((b) => b.addEventListener("click", () => {
    state.tf = b.dataset.tf; storageSet(TF_KEY, state.tf); state.firstLoad = true; state.analysis = null; press(); setText("mkt-tf", state.tf);
    state.annot = state.annotControls.sync(state.tf);
    refreshCandles(); refreshAnalysis();
  }));
  document.getElementById("series-select").addEventListener("change", (e) => {
    state.series = e.target.value || null; state.firstLoad = true; state.analysis = null; refreshCandles(); refreshAnalysis();
  });
  document.getElementById("chart-older").addEventListener("click", loadOlder);
  state.annotControls = bindAnnotationControls(() => state.tf, (settings, { sensitivityChanged }) => {
    state.annot = settings;
    if (sensitivityChanged) refreshAnalysis(); else applyOverlays();
  });
  state.annot = state.annotControls.sync(state.tf);
}

async function main() {
  document.getElementById("mkt-kind").replaceWith(kindBadge("current"));
  document.getElementById("acct-kind").replaceWith(kindBadge("simulated"));
  initControls();
  initImport();
  initProviderControls();
  let config = null;
  try {
    config = await api("/api/ui-config");
    setDisplayZone(config.display_timezone || "UTC");
    setText("chart-zone", `Times shown in ${config.display_timezone || "UTC"}`);
  } catch (_) { /* defaults */ }
  subscribeQuotes(renderQuote, (t) => setText("mkt-transport", t === "stream" ? "live updates (SSE)" : t));
  initTicket(() => state.chart);
  try { initTradingView(config); } catch (_) { /* optional panel stays hidden */ }
  await refreshSeriesList();
  await refreshCandles();
  refreshAnalysis();
  refreshAccount();
  refreshPerformance();
  setInterval(() => { if (!document.hidden) refreshPerformance(); }, 120000);
  setInterval(() => { if (!document.hidden) refreshCandles(); }, 15000);
  setInterval(() => { if (!document.hidden) refreshAnalysis(); }, 30000);
  setInterval(() => { if (!document.hidden) refreshSeriesList(); }, 60000);
  setInterval(() => { if (!document.hidden) refreshAccount(); }, 5000);
}

main();
