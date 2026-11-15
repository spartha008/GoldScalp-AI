// Performance analytics page: start evaluation runs, follow progress, show results.
// Analytical signal evaluation and the hypothetical simulated strategy are shown SEPARATELY.
import { api } from "./api.js";
import { createMultiLineChart } from "./chart.js";
import { signalBadge } from "./intel-panels.js";
import { announce, clear, el, fmtPrice, fmtTime, setDisplayZone, setText } from "./ui.js";

const STREAM_COLORS = { M1: "#60a5fa", M5: "#d4af37", M15: "#a78bfa" };
const state = { series: [], selected: null, poll: null, equity: null };
const toLocalInput = (iso) => iso.slice(0, 16);
const fromLocalInput = (value) => `${value}:00Z`;
const num = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));

function table(headers, rows, empty = "none") {
  if (!rows.length) return el("p", { class: "muted small", text: empty });
  return el("div", { class: "table-wrap" }, el("table", { class: "compact" }, [
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, rows),
  ]));
}

function stat(label, value, cls = "") {
  return el("div", { class: "stat" }, [el("div", { class: "label", text: label }), el("div", { class: `value num ${cls}`, text: value })]);
}

function pf(p) { return p.value !== null ? num(p.value, 2) : "n/a"; }

// ------------------------------------------------------------------ setup
async function loadSetup() {
  const [series, defaults] = await Promise.all([api("/api/market/series"), api("/api/performance/defaults")]);
  state.series = series.data.series;
  const select = clear(document.getElementById("pf-series"));
  for (const s of state.series) {
    const bars = Object.values(s.timeframes || {}).reduce((n, t) => n + t.bars, 0);
    select.append(el("option", { value: s.key, text: `${s.kind === "live" ? "Recorded live" : "Imported"}: ${s.key} (${bars} bars)` }));
  }
  // default: the dataset with the most stored candles (a full, unselected history)
  const total = (s) => Object.values(s.timeframes || {}).reduce((n, t) => n + t.bars, 0);
  const largest = [...state.series].sort((a, b) => total(b) - total(a))[0];
  if (largest) select.value = largest.key;
  const c = defaults.config;
  document.getElementById("pf-horizon").value = c.horizon_m5;
  document.getElementById("pf-target").value = c.target_r;
  document.getElementById("pf-stop").value = c.stop_atr_fallback;
  document.getElementById("pf-flat").value = c.flat_band_atr;
  document.getElementById("pf-lots").value = String(c.lots);
  document.getElementById("pf-spread").value = defaults.costs.configured_spread || "0.30";
  document.getElementById("pf-comm").value = defaults.costs.commission_per_lot_per_side;
  document.getElementById("pf-slip").value = defaults.costs.slippage;
  setText("perf-disclaimer", defaults.disclaimer);
  setText("run-busy", defaults.busy ? `running: ${defaults.busy}` : "");
  useFullRange();
}

/** The default range is EVERYTHING stored for the base timeframe: no hand-picked period. */
function useFullRange() {
  const s = state.series.find((x) => x.key === document.getElementById("pf-series").value);
  if (!s) return;
  const base = (s.timeframes || {}).M1 || (s.timeframes || {}).M5 || Object.values(s.timeframes || {})[0];
  if (!base) return;
  document.getElementById("pf-start").value = toLocalInput(base.first);
  document.getElementById("pf-end").value = toLocalInput(new Date(Date.parse(base.last) + 60000).toISOString());
  const parts = Object.entries(s.timeframes || {}).map(([tf, t]) => `${tf}: ${t.first.slice(0, 16)} → ${t.last.slice(0, 16)} UTC (${t.bars})`);
  setText("pf-coverage", `Stored candles · ${parts.join(" · ")}. M1 is the base when it covers the range.`);
}

async function submit(event) {
  event.preventDefault();
  const f = new FormData(event.target);
  const timeframes = f.getAll("tf");
  const out = document.getElementById("pf-status");
  if (!timeframes.length) { announce(out, "Choose at least one step timeframe.", "error"); return; }
  const body = {
    series: f.get("series"), timeframes, start: fromLocalInput(f.get("start")), end: fromLocalInput(f.get("end")),
    lookback_days: Number(f.get("lookback_days")), horizon_m5: Number(f.get("horizon_m5")), target_r: Number(f.get("target_r")),
    stop_atr_fallback: Number(f.get("stop_atr_fallback")), flat_band_atr: Number(f.get("flat_band_atr")),
    lots: f.get("lots"), spread: f.get("spread"), commission_per_lot_per_side: f.get("commission_per_lot_per_side"),
    slippage: f.get("slippage"),
  };
  try {
    const res = await api("/api/performance/runs", { method: "POST", body, timeoutMs: 60000 });
    announce(out, `Run ${res.run.id} started: ${res.run.total} steps.`, "ok");
    state.selected = res.run.id;
    poll();
  } catch (err) {
    announce(out, `Not started: ${err.message}`, "error");
  }
}

// ------------------------------------------------------------------ runs
async function refreshRuns() {
  const out = await api("/api/performance/runs");
  setText("run-busy", out.busy ? `running: ${out.busy}` : "");
  const rows = out.runs.map((r) => el("tr", { class: r.id === state.selected ? "here" : "" }, [
    el("td", { class: "num", text: fmtTime(r.created_at, true) }),
    el("td", { text: r.series_key }),
    el("td", { class: "small", text: `${(r.params.start || "").slice(0, 16)} → ${(r.params.end || "").slice(0, 16)}` }),
    el("td", { text: (r.params.timeframes || []).join(", ") }),
    el("td", { class: r.status === "failed" ? "down" : "", text: r.status === "running" ? `running ${r.progress}/${r.total}` : r.status }),
    el("td", {}, [
      el("button", { type: "button", class: "btn", text: "View", disabled: r.status !== "complete", onclick: () => { state.selected = r.id; showRun(r.id); refreshRuns(); } }),
      " ",
      r.status === "running"
        ? el("button", { type: "button", class: "btn", text: "Cancel", onclick: () => api(`/api/performance/runs/${r.id}/cancel`, { method: "POST" }).then(refreshRuns) })
        : el("button", { type: "button", class: "btn", text: "Delete", "aria-label": `Delete run ${r.id}`, onclick: () => deleteRun(r.id) }),
    ]),
  ]));
  const body = clear(document.getElementById("runs-body"));
  body.append(table(["Created", "Dataset", "Range (UTC)", "Streams", "Status", ""], rows, "No evaluation runs yet."));
  const failed = out.runs.find((r) => r.id === state.selected && r.status === "failed");
  if (failed) announce(document.getElementById("pf-status"), `Run failed: ${failed.error}`, "error");
  return out;
}

async function deleteRun(id) {
  try { await api(`/api/performance/runs/${id}`, { method: "DELETE" }); } catch (err) { announce(document.getElementById("pf-status"), err.message, "error"); }
  if (state.selected === id) state.selected = null;
  refreshRuns();
}

async function poll() {
  clearTimeout(state.poll);
  const out = await refreshRuns();
  const run = out.runs.find((r) => r.id === state.selected);
  const bar = document.getElementById("pf-progress");
  if (run && (run.status === "running" || run.status === "queued")) {
    bar.style.width = `${run.total ? Math.round((run.progress / run.total) * 100) : 0}%`;
    setText("pf-status", `Evaluating ${run.progress} of ${run.total} steps…`);
    state.poll = setTimeout(poll, 1500);
    return;
  }
  bar.style.width = run && run.status === "complete" ? "100%" : "0%";
  if (run && run.status === "complete") { announce(document.getElementById("pf-status"), "Evaluation complete.", "ok"); showRun(run.id); }
  else if (run && run.status === "cancelled") announce(document.getElementById("pf-status"), "Evaluation cancelled.", "warn");
}

// ------------------------------------------------------------------ results
function streamPanel(s) {
  const a = s.analytical;
  const st = s.strategy;
  const c = s.counts;
  const gated = Object.entries(c.gated_steps || {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "none";
  const outcomes = Object.entries(st.outcomes || {}).map(([k, v]) => `${k.replace("_", " ")} ${v}`).join(" · ") || "—";
  return el("section", { class: "panel" }, [
    el("header", {}, [el("h2", { text: `${s.timeframe} stream · ${s.steps} steps` }), el("span", { class: "spacer" }),
      el("span", { class: "muted small", text: `${fmtTime(s.first_step, true)} → ${fmtTime(s.last_step, true)}` })]),
    el("div", { class: "body" }, [
      el("h3", { text: "Signals" }),
      el("div", { class: "metric-grid" }, [
        stat("Total signals", c.total_signals), stat("Bullish", c.bullish, "trend-up"), stat("Bearish", c.bearish, "trend-down"),
        stat("Neutral observations", c.neutral_observations),
      ]),
      el("p", { class: "muted small", text: `Steps with no signal (gates): ${gated}` }),
      el("h3", { text: "A. Analytical evaluation (no costs)" }),
      el("div", { class: "metric-grid" }, [
        stat("Favourable", a.favourable, "trend-up"), stat("Adverse", a.adverse, "trend-down"), stat("Flat", a.flat),
        stat("Avg move (ATR)", num(a.avg_move_atr)), stat("Avg favourable (MFE)", `${num(a.avg_favourable)} · ${num(a.avg_favourable_atr)} ATR`),
        stat("Avg adverse (MAE)", `${num(a.avg_adverse)} · ${num(a.avg_adverse_atr)} ATR`),
        stat("Max MFE", `${num(a.max_favourable)} · ${num(a.max_favourable_atr)} ATR`), stat("Max MAE", `${num(a.max_adverse)} · ${num(a.max_adverse_atr)} ATR`),
      ]),
      el("p", { class: "muted small", text: `${a.label} Insufficient data: ${a.insufficient_data}. After neutral observations the average absolute move was ${num(a.neutral_avg_abs_move_atr)} ATR.` }),
      el("h3", {}, ["B. Hypothetical simulated strategy ", el("span", { class: "badge kind-simulated", text: "Simulated" })]),
      el("div", { class: "metric-grid" }, [
        stat("Trades", st.trades), stat("Wins / losses", `${st.wins} / ${st.losses}`),
        stat("Net P/L (USD)", fmtPrice(st.net_pnl), st.net_pnl > 0 ? "trend-up" : st.net_pnl < 0 ? "trend-down" : ""),
        stat("Profit factor", pf(st.profit_factor)), stat("Max drawdown", `${fmtPrice(st.max_drawdown)} (${num(st.max_drawdown_pct)}%)`),
        stat("Costs (USD)", `spread ${fmtPrice(st.total_spread_cost)} · comm ${fmtPrice(st.total_commission)} · slip ${fmtPrice(st.total_slippage_cost)}`),
      ]),
      el("p", { class: "muted small", text: `Exits: ${outcomes}. Skipped while a position was open: ${st.skipped_overlap}. ${st.profit_factor.reason ? `Profit factor ${st.profit_factor.reason}.` : ""} ${st.label}` }),
    ]),
  ]);
}

function groupTable(rows) {
  return table(["Group", "Signals", "Fav / adv / flat", "Avg move ATR", "Trades", "Net USD", "PF"], rows.map((g) => el("tr", {}, [
    el("td", { text: g.group }), el("td", { class: "num", text: g.signals }),
    el("td", { class: "num", text: `${g.favourable} / ${g.adverse} / ${g.flat}` }),
    el("td", { class: "num", text: num(g.avg_move_atr) }), el("td", { class: "num", text: g.trades }),
    el("td", { class: `num ${g.net_pnl > 0 ? "up" : g.net_pnl < 0 ? "down" : ""}`, text: fmtPrice(g.net_pnl) }),
    el("td", { class: "num", text: pf(g.profit_factor), title: g.profit_factor.reason || "" }),
  ])), "No signals.");
}

async function showRun(id) {
  let out;
  try { out = await api(`/api/performance/runs/${encodeURIComponent(id)}`); } catch (err) { announce(document.getElementById("pf-status"), err.message, "error"); return; }
  const run = out.run;
  const r = run.result;
  if (!r) return;
  setText("res-run", `${run.id} · config ${run.config_fingerprint.slice(0, 10)}`);
  const head = clear(document.getElementById("res-head"));
  const cov = r.coverage || {};
  head.append(el("p", { class: "small", text: `${r.series} · ${fmtTime(r.range.start, true)} → ${fmtTime(r.range.end, true)} · ${r.steps} steps · base ${r.base_tf}, ${cov.base_candles_in_range ?? "?"} candles in range`
    + (cov.gaps ? ` (${cov.gaps.missing} missing of ${cov.gaps.expected} expected)` : "") }));
  for (const w of r.warnings || []) head.append(el("p", { class: "notice warn", text: w }));
  head.append(el("p", { class: "muted small", text: r.disclaimer }));

  const streams = clear(document.getElementById("res-streams"));
  for (const s of r.streams) streams.append(streamPanel(s));

  if (!state.equity) state.equity = createMultiLineChart(document.getElementById("equity"));
  state.equity.setSeries(r.streams.map((s) => ({
    name: `${s.timeframe} (hypothetical)`, color: STREAM_COLORS[s.timeframe] || "#d4af37",
    points: [{ time: Math.floor(Date.parse(s.first_step) / 1000), value: s.strategy.equity.starting_balance },
             ...s.strategy.equity.points.map((p) => ({ time: Math.floor(Date.parse(p.time) / 1000), value: p.equity }))],
  })));
  setText("eq-note", `${r.streams[0].strategy.equity.basis}. Starting balance ${fmtPrice(r.streams[0].strategy.equity.starting_balance)} USD per stream.`);

  clear(document.getElementById("by-timeframe")).append(groupTable(r.by_timeframe));
  clear(document.getElementById("by-session")).append(groupTable(r.by_session));
  clear(document.getElementById("by-volatility")).append(groupTable(r.by_volatility));
  const as = clear(document.getElementById("assumptions"));
  const flat = Object.entries(r.assumptions).flatMap(([k, v]) => [el("dt", { text: k }),
    el("dd", { text: typeof v === "object" ? Object.entries(v).map(([a, b]) => `${a}: ${b}`).join("; ") : v })]);
  as.append(el("dl", { class: "kv small" }, flat));

  const rows = r.items.map((it) => el("tr", {}, [
    el("td", { class: "num", text: fmtTime(it.cutoff, true) }), el("td", { text: it.timeframe }),
    el("td", {}, signalBadge(it.direction)), el("td", { class: "num", text: it.score }),
    el("td", { class: "small", text: it.session }), el("td", { class: "small", text: it.volatility }),
    el("td", { class: "small", text: `${it.analytical.classification}${it.analytical.move_atr !== undefined && it.analytical.move_atr !== null ? ` (${num(it.analytical.move_atr)} ATR)` : ""}` }),
    el("td", { class: "small", text: it.trade.outcome.replace("_", " ") }),
    el("td", { class: `num ${it.trade.net_pnl > 0 ? "up" : it.trade.net_pnl < 0 ? "down" : ""}`, text: it.trade.net_pnl !== undefined ? fmtPrice(it.trade.net_pnl) : "—" }),
  ]));
  clear(document.getElementById("signal-list")).append(table(
    ["Cut-off", "TF", "Signal", "Score", "Session", "Volatility", "Analytical", "Hypothetical exit", "Net USD"], rows,
    "No directional signals in this run."));
}

async function main() {
  try { setDisplayZone((await api("/api/ui-config")).display_timezone || "UTC"); } catch (_) { /* UTC */ }
  document.getElementById("run-form").addEventListener("submit", submit);
  document.getElementById("pf-full").addEventListener("click", useFullRange);
  document.getElementById("pf-series").addEventListener("change", useFullRange);
  try { await loadSetup(); } catch (err) { announce(document.getElementById("pf-status"), `Could not load defaults: ${err.message}`, "error"); }
  const out = await refreshRuns();
  const running = out.runs.find((r) => r.status === "running" || r.status === "queued");
  const done = out.runs.find((r) => r.status === "complete");
  if (running) { state.selected = running.id; poll(); } else if (done) { state.selected = done.id; showRun(done.id); refreshRuns(); }
}

main();
