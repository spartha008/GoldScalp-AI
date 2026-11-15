// Replay page. The browser drives the clock (play/pause/speed); the server holds the cursor and
// returns the analysis as it was knowable at the replay time.
import { api } from "./api.js";
import { createCandleChart } from "./chart.js";
import { bindAnnotationControls } from "./annotations.js";
import {
  renderLiquidity, renderMTF, renderPriceAction, renderSessions, renderSignal, renderStructure, renderVolatility, signalBadge,
} from "./intel-panels.js";
import { announce, clear, el, fmtPrice, fmtTime, kindBadge, setDisplayZone, setText, storageGet, storageSet } from "./ui.js";

const state = { id: null, chart: null, playing: false, busy: false, overlays: null, firstLoad: true, series: [],
                direction: null, timer: null, tf: "M5", annot: null, annotControls: null };
const sensitivity = () => (state.annot && state.annot.sensitivity ? Number(state.annot.sensitivity) : null);

const toLocalInput = (iso) => iso.slice(0, 16);
const fromLocalInput = (value) => `${value}:00Z`;

function setButtons() {
  const ready = Boolean(state.id);
  document.getElementById("rp-play").disabled = !ready;
  document.getElementById("rp-step").disabled = !ready || state.playing;
  document.getElementById("rp-step10").disabled = !ready || state.playing;
  document.getElementById("rp-restart").disabled = !ready || state.playing;
  document.getElementById("rp-play").textContent = state.playing ? "Pause" : "Play";
}

async function loadSeries() {
  const select = document.getElementById("rp-series");
  const env = await api("/api/market/series");
  state.series = env.data.series;
  clear(select);
  for (const s of env.data.series) {
    const bars = Object.values(s.timeframes || {}).reduce((n, t) => n + t.bars, 0);
    select.append(el("option", { value: s.key, text: `${s.kind === "live" ? "Recorded live" : "Imported"}: ${s.key} (${bars} bars)` }));
  }
  const saved = storageGet("goldscalp:replay-series:v1");
  if (saved && env.data.series.some((s) => s.key === saved)) select.value = saved;
  else if (env.data.active) select.value = env.data.active;
  showCoverage(true);
}

function showCoverage(setDefaults = false) {
  const key = document.getElementById("rp-series").value;
  const s = state.series.find((x) => x.key === key);
  if (!s) { setText("rp-coverage", ""); return; }
  const parts = Object.entries(s.timeframes || {}).map(([tf, t]) => `${tf}: ${t.bars} bars ${t.first.slice(0, 16)} → ${t.last.slice(0, 16)} UTC`);
  setText("rp-coverage", `Stored candles · ${parts.join(" · ")}. M1 is used as the base when it covers the range; higher timeframes are rebuilt from it.`);
  if (setDefaults) {
    const base = (s.timeframes || {}).M1 || (s.timeframes || {}).M5 || Object.values(s.timeframes || {})[0];
    if (base) {
      const last = new Date(base.last);
      const start = new Date(Math.max(Date.parse(base.first), last.getTime() - 6 * 3600 * 1000));
      document.getElementById("rp-start").value = toLocalInput(start.toISOString());
      document.getElementById("rp-end").value = toLocalInput(last.toISOString());
    }
  }
}

function render(env) {
  const d = env.data;
  const rp = d.replay;
  state.id = rp.id;
  setText("rp-clock", fmtTime(d.cursor, true));
  document.getElementById("rp-progress").style.width = `${Math.round((rp.position / rp.total_steps) * 100)}%`;
  setText("rp-position", `Step ${rp.position} of ${rp.total_steps} (${rp.step_tf} candles) · base ${rp.base_tf}, ${rp.base_candles} candles from ${fmtTime(rp.first_base_candle, true)}`
    + (rp.finished ? " · end of the replay range" : ""));
  const empty = document.getElementById("chart-empty");
  if (!d.candles.length) { empty.hidden = false; empty.textContent = "No candles before the replay time."; }
  else {
    empty.hidden = true;
    if (!state.chart) state.chart = createCandleChart(document.getElementById("chart"));
    state.chart.setCandles(d.candles, { fit: state.firstLoad });
    state.firstLoad = false;
  }
  setText("chart-series", `${rp.series} · ${rp.step_tf} · replay ${fmtTime(rp.start, true)} → ${fmtTime(rp.end, true)}`);
  const a = d.analysis;
  state.overlays = a.overlays;
  applyOverlays();
  renderMTF(document.getElementById("mtf-body"), a.multi_timeframe);
  if (state.direction && state.direction !== a.signal.direction) {
    setText("sig-live", `Replay signal changed from ${state.direction} to ${a.signal.direction}.`);
  }
  state.direction = a.signal.direction;
  renderSignal(document.getElementById("sig-body"), a.signal, { prefill: false });
  renderStructure(document.getElementById("ms-body"), a.overlays);
  renderPriceAction(document.getElementById("pa-body"), a.overlays);
  renderLiquidity(document.getElementById("liq-body"), a.overlays ? a.overlays.liquidity : null,
                  a.overlays ? a.overlays.last_close : null);
  renderSessions(document.getElementById("ss-body"), a.sessions);
  renderVolatility(document.getElementById("vol-body"), a.volatility);
  renderHistory(d.signal_history, d.note);
  if (rp.finished && state.playing) { state.playing = false; setButtons(); }
}

function renderHistory(items, note) {
  const body = clear(document.getElementById("hist-body"));
  if (!items.length) { body.append(el("p", { class: "muted small", text: "No signal changes yet." })); return; }
  const rows = items.map((s) => {
    const ev = s.evaluation;
    const cells = (ev && ev.applicable ? ev.horizons : []).map((h) => h.status === "complete"
      ? `${h.m5_candles}×M5: move ${h.move_atr !== undefined ? `${h.move_atr} ATR` : fmtPrice(h.move)}, MFE ${h.mfe_atr ?? fmtPrice(h.mfe)}, MAE ${h.mae_atr ?? fmtPrice(h.mae)}`
      : `${h.m5_candles}×M5: pending`);
    return el("tr", {}, [
      el("td", { class: "num", text: fmtTime(s.cutoff, true) }),
      el("td", {}, signalBadge(s.direction)),
      el("td", { class: "num", text: `${s.score > 0 ? "+" : ""}${s.score}` }),
      el("td", { class: "small", text: cells.length ? cells.join(" · ") : "n/a (not directional)" }),
    ]);
  });
  body.append(el("div", { class: "table-wrap" }, el("table", { class: "outcome" }, [
    el("thead", {}, el("tr", {}, ["Cut-off", "Signal", "Score", "Forward outcome (historical observation)"].map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, rows),
  ])));
  body.append(el("p", { class: "muted small", text: "Move/MFE/MAE in ATR(M5) units from the signal's reference price, signed in the signal's direction. "
    + "Past behaviour in replayed data does not predict future results. " + (note || "") }));
}

function applyOverlays() {
  if (!state.chart || !state.annot) return;
  state.chart.setIntelligence(state.overlays, state.annot, { max: state.annot.max,
                                                             onDensity: (text) => setText("chart-density", text) });
}

async function send(path, body) {
  if (state.busy) return null;
  state.busy = true;
  try {
    const env = await api(path, { method: "POST", body, timeoutMs: 60000 });
    render(env);
    return env;
  } catch (err) {
    announce(document.getElementById("setup-result"), `Replay error: ${err.message}`, "error");
    state.playing = false;
    setButtons();
    return null;
  } finally {
    state.busy = false;
  }
}

async function playLoop() {
  while (state.playing) {
    const started = performance.now();
    const env = await send(`/api/replay/${state.id}/step`, { n: 1, swing_strength: sensitivity() });
    if (!env || env.data.replay.finished) { state.playing = false; break; }
    const speed = Number(document.getElementById("rp-speed").value) || 1;
    const wait = Math.max(0, 1000 / speed - (performance.now() - started));
    await new Promise((resolve) => { state.timer = setTimeout(resolve, wait); });
  }
  setButtons();
}

function initControls() {
  document.getElementById("setup-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    state.playing = false;
    if (state.id) { try { await api(`/api/replay/${state.id}`, { method: "DELETE" }); } catch (_) { /* expired */ } }
    state.id = null;
    state.firstLoad = true;
    state.direction = null;
    storageSet("goldscalp:replay-series:v1", form.get("series"));
    announce(document.getElementById("setup-result"), "Loading candles…");
    const env = await send("/api/replay", {
      series: form.get("series"), timeframe: form.get("timeframe"), start: fromLocalInput(form.get("start")),
      end: fromLocalInput(form.get("end")), lookback_days: Number(form.get("lookback_days")),
      swing_strength: sensitivity(),
    });
    if (env) announce(document.getElementById("setup-result"), "Replay loaded. Press Play or step one candle at a time.", "ok");
    setButtons();
  });
  document.getElementById("rp-series").addEventListener("change", () => showCoverage(true));
  document.getElementById("rp-play").addEventListener("click", () => {
    state.playing = !state.playing;
    setButtons();
    if (state.playing) playLoop(); else if (state.timer) clearTimeout(state.timer);
  });
  document.getElementById("rp-step").addEventListener("click", () => send(`/api/replay/${state.id}/step`, { n: 1, swing_strength: sensitivity() }));
  document.getElementById("rp-step10").addEventListener("click", () => send(`/api/replay/${state.id}/step`, { n: 10, swing_strength: sensitivity() }));
  document.getElementById("rp-restart").addEventListener("click", () => { state.firstLoad = true; send(`/api/replay/${state.id}/seek`, { position: 0, swing_strength: sensitivity() }); });
  document.getElementById("rp-tf").addEventListener("change", (e) => { state.tf = e.target.value; state.annot = state.annotControls.sync(state.tf); applyOverlays(); });
  state.annotControls = bindAnnotationControls(() => state.tf, async (settings, { sensitivityChanged }) => {
    state.annot = settings;
    if (sensitivityChanged && state.id && !state.playing) {
      const qs = settings.sensitivity ? `?swing_strength=${settings.sensitivity}` : "";
      try { render(await api(`/api/replay/${state.id}${qs}`, { timeoutMs: 60000 })); } catch (_) { /* keep the view */ }
    } else {
      applyOverlays();
    }
  }, "rannot");
  state.tf = document.getElementById("rp-tf").value;
  state.annot = state.annotControls.sync(state.tf);
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea") || !state.id) return;
    if (e.key === " ") { e.preventDefault(); document.getElementById("rp-play").click(); }
    if (e.key === "ArrowRight" && !state.playing) send(`/api/replay/${state.id}/step`, { n: 1, swing_strength: sensitivity() });
  });
}

async function main() {
  document.getElementById("top-kind").replaceWith(kindBadge("historical"));
  try {
    const config = await api("/api/ui-config");
    setDisplayZone(config.display_timezone || "UTC");
    setText("chart-zone", `Times shown in ${config.display_timezone || "UTC"} (inputs are UTC)`);
  } catch (_) { /* defaults */ }
  initControls();
  setButtons();
  try { await loadSeries(); } catch (err) { announce(document.getElementById("setup-result"), `Could not list datasets: ${err.message}`, "error"); }
}

main();
