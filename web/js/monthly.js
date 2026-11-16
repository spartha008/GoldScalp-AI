// Monthly Backtest: month selection, data availability, research candidates (data before the month), freezing the
// exact strategy versions, nine independent runs (A/B/C x M15/M5/M1) plus candidate runs, reports and exports.
// Historical simulation only - served by the separate research service; nothing here places orders.
import { api } from "./api.js";
import { createMultiLineChart } from "./chart.js";
import { announce, clear, el, fmtTime, setDisplayZone } from "./ui.js";

const S = { defaults: null, evalId: null, poll: null, charts: [] };
const num = (v, d = 2) => (v === null || v === undefined || v === "" ? "—" : Number(v).toFixed(d));
const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
const say = (msg, tone = "") => announce(document.getElementById("m-status"), msg, tone);
const NUMERIC = /^(—|[-+−]?\$?[\d.,]+(%| ?USD| ?R| min| lots| trades| days)?|[\d.,]+ (of|\/) [\d.,]+|\d+ \/ \d+)$/;
const td = (text, cls = "") => {                   // numbers align right; long free text wraps instead of stretching the table
  const t = text === null || text === undefined || text === "" ? "—" : String(text);
  return el("td", { class: [cls, NUMERIC.test(t) ? "n" : "", t.length > 56 ? "wrap" : ""].filter(Boolean).join(" ") || null, text: t });
};
const TONE = { COMPLETED: "ok", READY: "ok", ready: "ok", PASS: "ok", consistent: "ok", RUNNING: "run", RESEARCHING: "run",
  IMPORTING: "run", VALIDATING: "run", running: "run", "NOT STARTED": "muted", INCOMPLETE: "warn", WARN: "warn",
  CANCELLED: "warn", FAILED: "err", FAIL: "err" };
const chip = (s) => el("span", { class: `chip ${TONE[s] || "info"}`, text: String(s) });
const statusTd = (s) => el("td", { class: "left" }, s ? chip(s) : "—");
const pnl = (v) => td(v === null || v === undefined ? "—" : num(v), Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "");
const stat = (label, value) => el("div", { class: "stat" }, [el("div", { class: "label", text: label }),
  el("div", { class: "value num", text: value === null || value === undefined ? "—" : String(value) })]);
const link = (href, text) => el("a", { class: "btn", href, text });

function table(headers, rows, empty = "none") {
  if (!rows.length) return el("p", { class: "muted small", text: empty });
  // a column whose cells are all numeric gets a right-aligned header too
  const numericCol = (i) => rows.every((r) => !r.children[i] || r.children[i].classList.contains("n"));
  return el("div", { class: `table-wrap${rows.length > 14 ? " scroll" : ""}` }, el("table", { class: "compact data" }, [
    el("thead", {}, el("tr", {}, headers.map((h, i) => el("th", { scope: "col", class: i > 0 && numericCol(i) ? "n" : null, text: h })))),
    el("tbody", {}, rows),
  ]));
}

function bars(items, label, value) {           // simple horizontal bar list (CSSOM widths, no inline style attributes)
  const max = Math.max(1, ...items.map(value).map(Math.abs));
  return el("div", { class: "m-bars" }, items.map((it) => {
    const bar = el("span", { class: `m-bar ${value(it) < 0 ? "neg" : ""}` });
    bar.style.width = `${Math.round((Math.abs(value(it)) / max) * 100)}%`;
    return el("div", { class: "m-bar-row" }, [el("span", { class: "m-bar-label small", text: label(it) }), el("span", { class: "m-bar-track" }, bar),
      el("span", { class: "small num", text: String(value(it)) })]);
  }));
}

// ------------------------------------------------------------------ setup
function params() {
  const v = (id) => document.getElementById(id).value.trim();
  const [store, series] = v("m-series").split("|");
  const body = { month: v("m-month"), store, series, research_days: Number(v("m-rdays")), configured_spread: v("m-spread"),
                 commission: v("m-comm"), slippage: v("m-slip") };
  if (v("m-riskpct")) body.risk_percent_override = Number(v("m-riskpct"));
  return body;
}

async function loadDefaults() {
  const d = await api("/api/monthly/defaults", { timeoutMs: 120000 });
  S.defaults = d;
  document.getElementById("m-month").value = d.default_month || "";
  const sel = clear(document.getElementById("m-series"));
  for (const s of d.series_list) {
    const tfs = Object.entries(s.timeframes).map(([tf, t]) => `${tf} ${t.first.slice(0, 10)}→${t.last.slice(0, 10)}`).join(", ");
    sel.append(el("option", { value: `${s.store}|${s.key}`, text: `${s.label}: ${tfs}` }));
  }
  sel.value = `${d.store}|${d.series}`;
  document.getElementById("m-spread").value = d.assumptions.configured_spread;
  document.getElementById("m-comm").value = d.assumptions.commission;
  document.getElementById("m-slip").value = d.assumptions.slippage;
  const c = d.contract;
  clear(document.getElementById("m-defaults")).append(
    el("p", { class: "small", text: `Default month: ${d.default_month || "none with sufficient M15 and M5 data"} (latest completed month with enough data${d.skipped_months.length ? `; skipped ${d.skipped_months.map((s) => s.month).join(", ")}` : ""}). Starting balance $${d.assumptions.starting_balance}, hypothetical leverage 1:${d.assumptions.leverage} (margin only), timeframes ${d.assumptions.timeframes.join(" / ")}, ${d.assumptions.execution_model}.` }),
    el("p", { class: "small", text: c.verified ? `Contract: ${c.contract_size} oz per lot, lots ${c.min_lot}-${c.max_lot} step ${c.lot_step}, tick ${c.tick_size} (from actual MT5 terminal verification ${c.record}); tick value $${c.tick_value_per_lot} per lot is derived. ${c.margin_note}` : `Contract: ${c.note}` }),
    table(["Strategy", "Version", "Fingerprint", "Sizing", "Stop", "Take-profit", "Risk-to-reward"],
      d.strategies.map((s) => el("tr", {}, [td(`${s.key}: ${s.name}`), td(`v${s.version}`), td(s.fingerprint.slice(0, 12)),
        td(`${s.readiness.sizing.mode}${s.readiness.sizing.lots ? ` ${s.readiness.sizing.lots} lots` : ""}`),
        td(`${s.readiness.stop.type} ${s.readiness.stop.timeframe}`), td(`${s.readiness.target.type} ${s.readiness.target.r ?? ""}`),
        td(s.readiness.rr.note, s.readiness.rr.in_range ? "up" : "down")]))));
}

async function checkAvailability() {
  const p = params();
  const av = await api(`/api/monthly/availability?month=${encodeURIComponent(p.month)}&store=${encodeURIComponent(p.store)}&series=${encodeURIComponent(p.series)}`, { timeoutMs: 120000 });
  renderAvailability(av);
}

function renderAvailability(av) {
  const box = clear(document.getElementById("m-availability"));
  box.append(el("p", { class: "small", text: `${av.month}: ${av.range.start} to ${av.range.end} (UTC), ${av.series} (${av.store} store).` }),
    table(["Run timeframe", "Execution candles", "Coverage", "Analysis base", "History before month", "Status"],
      Object.entries(av.timeframes).map(([tf, t]) => el("tr", {}, [td(tf), td(`${t.execution.present} of ${t.execution.expected}`),
        td(pct(t.execution.share)), td(t.analysis_base), td(t.history_from ? t.history_from.slice(0, 16) : "none"),
        t.sufficient ? statusTd("ready") : td(t.problems.join("; "), "down wrap")]))),
    el("p", { class: "small", text: `Research window (before the month): ${av.research.window.start} to ${av.research.window.end}, ${av.research.days_available} days, coverage ${pct(av.research.coverage.share)} - ${av.research.sufficient ? "sufficient" : av.research.reason}.` }));
}

async function createEvaluation() {
  try {
    const ev = await api("/api/monthly/evaluations", { method: "POST", body: params(), timeoutMs: 120000 });
    say(`Evaluation ${ev.id} created (${ev.status}).`, "ok");
    await listEvaluations();
    openEvaluation(ev.id);
  } catch (err) { say(err.message, "error"); }
}

async function listEvaluations() {
  const out = await api("/api/monthly/evaluations");
  clear(document.getElementById("m-evaluations")).append(table(["Created", "Evaluation", "Month", "Data", "Sizing", "Status", "Phase", ""],
    out.evaluations.map((e) => el("tr", {}, [td(fmtTime(e.created_at, true)), td(e.id), td(e.month), td(`${e.params.series} (${e.params.store})`),
      td(e.params.risk_scenario ? `HYPOTHETICAL ${e.params.risk_scenario.risk_percent}% of equity (study ${e.params.risk_study})` : e.params.risk_percent_override ? `risk override ${e.params.risk_percent_override}%` : "baseline: configured fixed lots",
        e.params.risk_scenario ? "down" : ""), statusTd(e.status), td(e.phase || (e.research_done ? "research done" : "")),
      el("td", {}, el("button", { type: "button", class: "btn sm", text: "Open", onclick: () => openEvaluation(e.id) }))])),
    "No evaluation yet."));
}

// ------------------------------------------------------------------ evaluation
async function action(path, label) {
  try {
    await api(path, { method: "POST", timeoutMs: 120000 });
    say(`${label} started.`, "ok");
    openEvaluation(S.evalId);
  } catch (err) { say(err.message, "error"); }
}

function openEvaluation(id) {
  S.evalId = id;
  clearInterval(S.poll);
  refreshEvaluation();
  S.poll = setInterval(refreshEvaluation, 3000);
}

async function refreshEvaluation() {
  if (!S.evalId) return;
  const ev = await api(`/api/monthly/evaluations/${S.evalId}`);
  document.getElementById("m-current-panel").hidden = false;
  clear(document.getElementById("m-badge")).append(chip(ev.status));
  document.getElementById("m-summary").textContent = `${ev.id}: ${ev.month} (${ev.range_start} to ${ev.range_end}), ${ev.params.series} (${ev.params.store}), research ${ev.params.research_days} days, app ${ev.app_version}.`
    + (ev.error ? ` Error: ${ev.error}` : "");
  document.getElementById("m-progress").textContent = ev.phase ? `${ev.phase}: ${ev.progress}${ev.total ? ` / ${ev.total}` : " steps"}` : "";
  const acts = clear(document.getElementById("m-actions"));
  const busy = ["RESEARCHING", "RUNNING"].includes(ev.status);
  if (busy) acts.append(el("button", { type: "button", class: "btn", text: "Cancel", onclick: () => action(`/api/monthly/evaluations/${ev.id}/cancel`, "Cancel") }));
  if (["NOT STARTED", "FAILED"].includes(ev.status) && !ev.frozen) acts.append(el("button", { type: "button", class: ev.research_done ? "btn" : "btn primary", text: ev.research_done ? "Re-run research" : "Run research", onclick: () => action(`/api/monthly/evaluations/${ev.id}/research`, "Research") }));
  if (ev.status === "NOT STARTED" && ev.research_done) acts.append(el("button", { type: "button", class: "btn primary", text: "Freeze strategy versions", onclick: () => action(`/api/monthly/evaluations/${ev.id}/freeze`, "Freeze") }));
  if (ev.status === "READY" && !ev.params.risk_study) acts.append(el("button", { type: "button", class: "btn primary", text: "Start the nine backtests (+ candidates)", onclick: () => action(`/api/monthly/evaluations/${ev.id}/run`, "Runs") }));
  renderResearch(ev);
  renderFrozen(ev);
  renderRuns(ev);
  renderExports(ev);
  if (!busy) { clearInterval(S.poll); S.poll = null; }
}

function renderResearch(ev) {
  const box = clear(document.getElementById("m-research"));
  if (!ev.research_done) { box.append(el("p", { class: "muted small", text: ev.status === "RESEARCHING" ? "Researching..." : "Not run yet." })); return; }
  const w = ev.research.window;
  box.append(el("p", { class: "small", text: `Research period ${w.start} to ${w.end} (development until ${w.development_end}, then check). ${ev.research.note}` }),
    table(["Candidate", "Concept", "Chosen RR", "Dev 1.5R (trades / net / exp R)", "Dev 2.0R", "Check (trades / net)", "Why", "Flag"],
      ev.candidates.map((c) => {
        const v = Object.fromEntries(c.research.variants.map((x) => [x.rr, x.development]));
        const f = (d) => (d ? `${d.trades} / ${num(d.net)} / ${num(d.expectancy_r, 3)}` : "—");
        return el("tr", {}, [td(c.name), td(c.concept), td(`1:${c.research.chosen_rr}`), td(f(v[1.5])), td(f(v[2])),
          td(`${c.research.check.trades} / ${num(c.research.check.net)}`), td(c.research.chosen_by), td(c.research.flag)]);
      })),
    ...ev.candidates.map((c) => el("details", {}, [el("summary", { text: `${c.name}: rules, limitations, data` }),
      el("ul", { class: "plain" }, Object.entries(c.documentation).map(([k, val]) => el("li", { text: `${k}: ${Array.isArray(val) ? val.join("; ") : val}` })))])));
}

function renderFrozen(ev) {
  const box = clear(document.getElementById("m-frozen"));
  if (!ev.frozen) { box.append(el("p", { class: "muted small", text: "Not frozen yet." })); return; }
  const a = ev.frozen.assumptions;
  box.append(el("p", { class: "small", text: `Frozen ${ev.frozen.at}. Engine assumptions: balance $${a.engine.starting_balance}, leverage 1:${a.engine.leverage}, spread fallback ${a.engine.configured_spread}, commission ${a.engine.commission_per_lot_per_side}/lot/side, slippage ${a.engine.slippage}/oz, intrabar ${a.engine.intrabar}${a.engine.sizing_override ? `, RISK OVERRIDE ${a.engine.sizing_override.risk_percent}%` : ""}.` }),
    table(["Key", "Strategy (exact version)", "Fingerprint", "Sizing", "Stop", "Take-profit", "Max hold", "Risk-to-reward"],
      ev.frozen.strategies.map((s) => el("tr", {}, [td(s.key), td(`${s.name} v${s.version}`), td(s.fingerprint.slice(0, 12)),
        td(`${s.readiness.sizing.mode}${s.readiness.sizing.lots ? ` ${s.readiness.sizing.lots}` : ""}`),
        td(`${s.readiness.stop.type} ${s.readiness.stop.timeframe}${s.readiness.stop.type === "atr" ? ` x${s.readiness.stop.atr_multiple}` : ""}`),
        td(`${s.readiness.target.type} ${s.readiness.target.r ?? ""}`), td(`${s.readiness.max_holding_minutes} min`),
        td(s.readiness.rr.note, s.readiness.rr.in_range ? "up" : "down")]))));
}

function renderRuns(ev) {
  const box = clear(document.getElementById("m-runs"));
  if (!ev.runs.length) { box.append(el("p", { class: "muted small", text: "No runs yet." })); return; }
  for (const kind of ["existing", "candidate"]) {
    const keys = [...new Set(ev.runs.filter((r) => r.kind === kind).map((r) => r.strategy_key))];
    if (!keys.length) continue;
    box.append(el("p", { class: "small", text: kind === "existing" ? "Strategies A / B / C (exact frozen versions):" : "Research candidates (separate section):" }),
      table(["Strategy", "M15", "M5", "M1", "Comparison"], keys.map((k) => el("tr", {}, [td(k),
        ...["M15", "M5", "M1"].map((tf) => {
          const r = ev.runs.find((x) => x.strategy_key === k && x.timeframe === tf);
          if (!r) return td("—");
          const s = r.summary;
          const cell = el("td", { class: r.status === "COMPLETED" ? (s && s.net > 0 ? "up" : s && s.net < 0 ? "down" : "") : "" });
          cell.append(el("div", { class: "small", text: r.status === "COMPLETED" ? `${s.trades} trades, ${num(s.net)} USD` : r.status }));
          if (r.status === "COMPLETED") cell.append(el("button", { type: "button", class: "btn", text: "Report", onclick: () => openRun(r.id) }));
          else if (r.reason) cell.append(el("div", { class: "muted small", text: r.reason.slice(0, 120) }));
          return cell;
        }),
        el("td", {}, el("button", { type: "button", class: "btn", text: "M15 vs M5 vs M1", onclick: () => openStrategy(ev.id, k) }))]))));
  }
}

function renderExports(ev) {
  const box = clear(document.getElementById("m-exports"));
  if (!["COMPLETED", "INCOMPLETE"].includes(ev.status)) { box.append(el("p", { class: "muted small", text: "Available when the runs finish." })); return; }
  box.append(el("div", { class: "toolbar" }, [
    el("button", { type: "button", class: "btn", text: "Open combined report", onclick: () => openCombined(ev.id) }),
    link(`/api/monthly/evaluations/${ev.id}/report.pdf`, "Combined PDF"), link(`/api/monthly/evaluations/${ev.id}/summary.csv`, "Combined CSV"),
    ...[...new Set(ev.runs.filter((r) => r.kind === "existing").map((r) => r.strategy_key))].map((k) => link(`/api/monthly/evaluations/${ev.id}/strategies/${k}/report.pdf`, `Strategy ${k} PDF`)),
  ]));
}

// ------------------------------------------------------------------ reports
function closeReport() {
  for (const c of S.charts) c.dispose();
  S.charts = [];
  document.getElementById("m-report-panel").hidden = true;
}

function showReport(title, links, nodes) {
  for (const c of S.charts) c.dispose();
  S.charts = [];
  document.getElementById("m-report-panel").hidden = false;
  document.getElementById("rep-title").textContent = title;
  clear(document.getElementById("m-report-links")).append(...links);
  clear(document.getElementById("m-report")).append(...nodes);
  document.getElementById("m-report-panel").scrollIntoView({ behavior: "smooth" });
}

function lineChart(series, height = 260) {
  const box = el("div", { class: "equity-box" });
  box.style.height = `${height}px`;
  setTimeout(() => {
    const chart = createMultiLineChart(box);
    chart.setSeries(series.map((s) => ({ name: s.name, color: s.color, points: s.points.map((p) => ({ time: Math.floor(Date.parse(p.time) / 1000), value: p.value })) })));
    S.charts.push(chart);
  }, 30);
  return box;
}

async function openRun(rid) {
  const { run, evaluation } = await api(`/api/monthly/runs/${rid}`);
  const r = run.result;
  const p = r.performance, k = r.risk, c = r.costs, ds = run.dataset;
  const reproduce = el("button", { type: "button", class: "btn", text: "Reproduce", onclick: async (e) => {
    e.target.disabled = true;
    try {
      const out = await api(`/api/monthly/runs/${rid}/reproduce`, { method: "POST", timeoutMs: 900000 });
      say(out.identical ? `Reproduced: identical result hash ${out.reproduced_result_hash.slice(0, 16)}.` : `Different result (${out.note || "check inputs"}).`, out.identical ? "ok" : "error");
    } catch (err) { say(err.message, "error"); } finally { e.target.disabled = false; }
  } });
  showReport(`${run.config.name} — ${run.timeframe} — ${evaluation.month}`,
    [link(`/api/monthly/runs/${rid}/report.pdf`, "PDF"), link(`/api/monthly/runs/${rid}/ledger.csv`, "CSV ledger"), link(`/api/monthly/runs/${rid}/run.json`, "JSON"), reproduce], [
      el("p", { class: "notice warn", text: "Hypothetical historical simulation; one month is not evidence of reliability or of future results." }),
      el("p", { class: "small", text: `Run ${run.id}: ${run.kind} strategy, fingerprint ${run.fingerprint.slice(0, 16)}; XAUUSD ${run.timeframe} decisions and execution; data ${ds.series} (${ds.store}), analysis base ${ds.analysis_base}, ${ds.execution_candles} execution candles ${ds.execution_first} to ${ds.execution_last}, spread known on ${pct(ds.spread_known_share)} of candles; dataset hash ${ds.dataset_hash.slice(0, 12)}; result hash ${run.result_hash.slice(0, 12)}.` }),
      el("div", { class: "metric-grid" }, [stat("Trades (W/L)", `${p.trades} (${p.wins}/${p.losses})`), stat("Win rate", pct(p.win_rate)), stat("Net after costs", `${num(p.net)} USD`),
        stat("Return", `${num(p.return_pct)}%`), stat("Ending balance", `${num(p.ending_balance)}`), stat("Profit factor", num(p.profit_factor, 2)),
        stat("Gross profit / loss", `${num(p.gross_profit)} / ${num(p.gross_loss)}`), stat("Avg win / loss", `${num(p.average_win)} / ${num(p.average_loss)}`),
        stat("Expectancy", `${num(p.expectancy_usd)} USD`), stat("Average R", num(p.average_r, 3)), stat("Max drawdown", `${num(p.max_drawdown)} (${num(p.max_drawdown_pct)}%)`),
        stat("Largest win / loss", `${num(p.largest_win)} / ${num(p.largest_loss)}`), stat("Avg holding", `${num(p.average_holding_minutes, 1)} min`)]),
      p.small_sample ? el("p", { class: "notice warn", text: p.sample_note }) : null,
      el("div", { class: "metric-grid" }, [stat("Avg / total risk", `${num(k.average_risk_usd)} / ${num(k.total_risk_usd)} USD`), stat("Avg risk % balance", `${num(k.average_risk_pct_of_balance)}%`),
        stat("Planned RR (avg)", k.planned_rr_average ? `1:${num(k.planned_rr_average)}` : "—"), stat("Realized R (avg)", num(k.realized_r_average, 3)),
        stat("Stop / TP / other exits", `${k.stop_loss_exits} / ${k.take_profit_exits} / ${k.other_exits}`), stat("Rejected entries", `${k.rejected_entries}`),
        stat("Margin rejections", k.margin_rejections), stat("Max used margin", `${num(k.max_used_margin)} (${num(k.max_margin_pct_of_equity)}% eq.)`),
        stat("Costs: spread / comm. / slip.", `${num(c.spread)} / ${num(c.commission)} / ${num(c.slippage)}`), stat("Costs vs |gross|", c.share_of_abs_gross_pct === null ? "—" : `${c.share_of_abs_gross_pct}%`)]),
      el("p", { class: "muted small", text: k.risk_note }),
      ...(k.percentage_risk ? [
        el("p", { class: "notice error", text: S.defaults ? S.defaults.risk_banner : "High-risk hypothetical simulation scenario." }),
        el("h3", { text: `Percentage risk sizing: ${k.percentage_risk.risk_percent}% of current simulated equity (hypothetical)` }),
        el("p", { class: "small", text: k.percentage_risk.formula }),
        el("div", { class: "metric-grid" }, [stat("Risk budget avg / min / max", `${num(k.percentage_risk.average_risk_budget_usd)} / ${num(k.percentage_risk.min_risk_budget_usd)} / ${num(k.percentage_risk.max_risk_budget_usd)}`),
          stat("Planned risk avg / max", `${num(k.percentage_risk.average_planned_risk_usd)} / ${num(k.percentage_risk.max_planned_risk_usd)}`),
          stat("Lots avg / min / max", `${num(k.percentage_risk.average_lots, 3)} / ${k.percentage_risk.min_lots} / ${k.percentage_risk.max_lots}`),
          stat("Stop-loss losses", `${num(k.percentage_risk.stop_loss_losses_usd)} USD`), stat("Losses beyond plan", `${k.percentage_risk.losses_beyond_plan} (${num(k.percentage_risk.losses_beyond_plan_usd)} USD)`),
          stat("Budget compliance", k.percentage_risk.compliance), stat("Min-volume rejections", k.percentage_risk.insufficient_volume_rejections),
          stat("Margin rejections", k.percentage_risk.margin_rejections), stat("Capped entries", k.percentage_risk.capped_entries), stat("Longest losing streak", p.longest_losing_streak)]),
        el("p", { class: "muted small", text: k.percentage_risk.note }),
        el("h3", { text: "Position size (lots) and risk budget per entry" }),
        lineChart([{ name: "lots", color: "#a78bfa", points: r.charts.position_size }], 140),
        lineChart([{ name: "budget", color: "#d4af37", points: r.charts.risk_budget }, { name: "planned risk", color: "#60a5fa", points: r.charts.planned_risk }], 160),
      ] : []),
      el("h3", { text: "Equity and balance" }), lineChart([{ name: "equity", color: "#d4af37", points: r.charts.equity }, { name: "balance", color: "#60a5fa", points: r.charts.balance }]),
      el("h3", { text: "Drawdown" }), lineChart([{ name: "drawdown", color: "#e5484d", points: r.charts.drawdown }], 160),
      ...(r.charts.trade_outcomes ? [el("h3", { text: "Trade outcomes (net USD, exit order)" }), bars(r.charts.trade_outcomes.map((x, i) => ({ ...x, i: i + 1 })), (d) => `#${d.i} ${d.time.slice(5, 16)}`, (d) => d.value)] : []),
      el("h3", { text: "Trades per day (net USD)" }), bars(r.charts.daily, (d) => `${d.day} (${d.trades})`, (d) => d.net),
      el("h3", { text: "Win/loss distribution (R)" }), bars(r.charts.r_distribution, (d) => d.bucket, (d) => d.trades),
      el("h3", { text: "Interpretation" }), el("ul", { class: "plain" }, r.interpretation.map((x) => el("li", { text: x }))),
      el("h3", { text: "Trade history" }), table(["#", "Side", "Entry", "Price", "Stop", "Target", "RR", "Risk $", "Exit", "Reason", "Net", "R"],
        r.trades.map((t) => el("tr", {}, [td(t.id), td(t.side), td(t.entry_time.slice(5, 16)), td(num(t.entry_price)), td(num(t.stop)), td(num(t.target)),
          td(t.planned_rr ? `1:${num(t.planned_rr)}` : "—"), td(num(t.risk_usd)), td(t.exit_time.slice(5, 16)), td(t.exit_reason), pnl(t.net_pnl), td(num(t.r_multiple))]))),
      el("h3", { text: "Rejected entries" }), table(["Time", "Side", "Reason", "Detail"], r.rejections.map((x) => el("tr", {}, [td(x.time.slice(5, 16)), td(x.side), td(x.reason),
        td(Object.entries(x).filter(([kk]) => !["time", "side", "reason", "setup_cutoff"].includes(kk)).map(([kk, vv]) => `${kk} ${vv}`).join(", "))])), "No rejected entries."),
    ]);
}

async function openStrategy(eid, key) {
  const out = await api(`/api/monthly/evaluations/${eid}/strategies/${key}`);
  const c = out.comparison;
  showReport(`${out.name}: M15 vs M5 vs M1`, [link(`/api/monthly/evaluations/${eid}/strategies/${key}/report.pdf`, "PDF")], [
    table(["TF", "Status", "Trades", "Net", "Return %", "Win rate", "PF", "Expectancy", "Max DD", "Costs", "Avg risk"],
      c.rows.map((x) => el("tr", {}, [td(x.timeframe), td(x.status), td(x.trades), pnl(x.net), td(num(x.return_pct)), td(pct(x.win_rate)), td(num(x.profit_factor, 2)),
        td(num(x.expectancy_usd)), td(num(x.max_drawdown)), td(num(x.costs)), td(num(x.average_risk_usd))]))),
    el("ul", { class: "plain" }, c.assessment.map((x) => el("li", { text: x }))),
  ]);
}

async function openCombined(eid) {
  const c = await api(`/api/monthly/evaluations/${eid}/combined`);
  showReport(`Combined report: ${c.evaluation.month}`, [link(`/api/monthly/evaluations/${eid}/report.pdf`, "PDF"), link(`/api/monthly/evaluations/${eid}/summary.csv`, "CSV")], [
    el("p", { class: "notice warn", text: c.separate_accounts_note }),
    table(["Strategy", "TF", "Status", "Trades", "Net", "Return %", "PF", "Expectancy", "Max DD", "Costs"],
      c.table.map((t) => el("tr", {}, [td(c.names[t.strategy] || t.strategy), td(t.timeframe), td(t.status), td(t.trades), pnl(t.net), td(num(t.return_pct)),
        td(num(t.profit_factor, 2)), td(num(t.expectancy_usd)), td(num(t.max_drawdown)), td(num(t.costs))]))),
    el("p", { class: "small", text: `Sum of ${c.completed_runs} separate accounts (not one account): ${num(c.sum_of_separate_accounts_net)} USD.` }),
    el("h3", { text: "Key findings" }), el("ul", { class: "plain" }, c.findings.map((x) => el("li", { text: x }))),
    c.incomplete.length ? el("h3", { text: "Incomplete runs and data limitations" }) : null,
    c.incomplete.length ? el("ul", { class: "plain" }, c.incomplete.map((t) => el("li", { text: `${t.strategy} ${t.timeframe}: ${t.status} - ${t.reason}` }))) : null,
    el("h3", { text: "Research candidates (research period vs evaluation month)" }),
    table(["Candidate", "RR", "Research check", "Month M15", "Month M5", "Month M1", "Flag"], c.candidates.map((x) => el("tr", {}, [td(x.name), td(`1:${x.chosen_rr}`),
      td(`${x.research_check_trades} trades / ${num(x.research_check_net)}`),
      ...["M15", "M5", "M1"].map((tf) => td(x.month[tf] ? `${x.month[tf].trades} / ${num(x.month[tf].net)}` : "—")), td(x.flag)]))),
    el("h3", { text: "Conclusion" }), el("p", { class: "small", text: c.conclusion }),
  ]);
}

// ------------------------------------------------------------------ 1. historical data import (Phase 5.1)
const STATES = ["NOT STARTED", "IMPORTING", "VALIDATING", "INCOMPLETE", "READY", "RUNNING", "COMPLETED", "FAILED"];
function setBadge(id, state) {
  const b = document.getElementById(id);
  b.textContent = state;
  b.className = `badge ${["READY", "COMPLETED"].includes(state) ? "ok" : ["FAILED", "INCOMPLETE"].includes(state) ? "warn" : ""}`.trim();
  if (!STATES.includes(state) && state !== "—") b.className = "badge";
}

function importForm(includeLabel = true) {
  const v = (id) => document.getElementById(id).value.trim();
  const form = new FormData();
  const f = document.getElementById("i-file").files[0];
  if (f) form.append("file", f);
  for (const [k, id] of [["instrument", "i-instrument"], ["timeframe", "i-tf"], ["dialect", "i-dialect"], ["time_basis", "i-basis"],
    ["point", "i-point"], ["series_label", "i-series"], ["month", "i-month"]]) form.append(k, v(id));
  if (includeLabel) form.append("label", v("i-label"));
  return form;
}

function upload(path, form, onProgress) {           // XMLHttpRequest for upload progress; same CSRF header as api()
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.setRequestHeader("X-GoldScalp-Request", "1");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.timeout = 900000;
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100)); };
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* non-JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error((body && (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail))) || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("server unreachable"));
    xhr.ontimeout = () => reject(new Error("request timed out"));
    xhr.send(form);
  });
}

function checksTable(checks) {
  return table(["Check", "Result", "Detail"], checks.map((c) => el("tr", {}, [td(c.check), statusTd(c.status), td(c.detail, "left")])));
}

function coverageNodes(cov) {
  return [
    el("div", { class: "metric-grid" }, [stat(`${cov.month} ${cov.timeframe} bars`, `${cov.present} of ${cov.expected}`), stat("Coverage", pct(cov.share)),
      stat("Missing bars", cov.missing), stat("Needed", pct(cov.min_coverage)), stat("History before month from", cov.history_before_month_from ? cov.history_before_month_from.slice(0, 16) : "none"),
      stat("Warm-up", cov.warmup.satisfied ? "satisfied" : "NOT satisfied"), stat("First / last in month", `${(cov.first_in_month || "—").slice(5, 16)} / ${(cov.last_in_month || "—").slice(5, 16)}`),
      ...(cov.total_candles !== undefined ? [stat("Stored candles (all)", cov.total_candles), stat("Earliest / latest", `${(cov.earliest || "—").slice(0, 10)} / ${(cov.latest || "—").slice(0, 10)}`)] : [])]),
    el("p", { class: "small muted", text: cov.warmup.rule + `; needed from ${cov.warmup.needed_from}.` }),
    el("details", {}, [el("summary", { text: `Missing dates (${cov.missing_dates.length}) and intervals (${cov.missing_intervals_total})` }),
      table(["Date", "Missing bars"], cov.missing_dates.map((d) => el("tr", {}, [td(d.date), td(d.bars)])), "No missing dates."),
      table(["From", "To", "Bars"], cov.missing_intervals.map((m) => el("tr", {}, [td(m.from), td(m.to), td(m.bars)])), "No missing intervals.")]),
  ];
}

async function previewImport() {
  const box = clear(document.getElementById("i-result"));
  document.getElementById("i-import").disabled = true;
  if (!document.getElementById("i-file").files[0]) { say("Choose a CSV file first.", "error"); return; }
  setBadge("i-state", "VALIDATING");
  try {
    const out = await upload("/api/monthly/import/preview", importForm(false), () => {});
    box.append(el("p", { class: "small", text: `${out.file_name} (sha ${out.sha256}): ${out.rows_read} rows, ${out.rows_valid} valid, ${out.rows_rejected} rejected, ${out.duplicates_dropped} duplicates; ${out.first} to ${out.last} UTC.${out.already_imported ? " This exact file is ALREADY imported in this series: importing again changes nothing." : ""} ${out.note}` }),
      checksTable(out.checks),
      el("h3", { text: "Timestamp conversion (first rows)" }),
      table(["Source row", "UTC", "Open", "High", "Low", "Close", "Spread USD"], out.samples.map((s) => el("tr", {}, [td(s.source_row.replaceAll("\t", " ")), td(s.utc), td(s.open), td(s.high), td(s.low), td(s.close), td(s.spread_usd)]))),
      ...coverageNodes(out.coverage));
    document.getElementById("i-import").disabled = !out.importable;
    setBadge("i-state", out.importable ? (out.coverage.ready ? "READY" : "INCOMPLETE") : "FAILED");
    if (!out.importable) say("The file failed validation; nothing can be imported until the failing checks are fixed.", "error");
  } catch (err) { setBadge("i-state", "FAILED"); say(err.message, "error"); }
}

async function runImport() {
  const bar = document.getElementById("i-progress");
  bar.hidden = false; bar.value = 0;
  setBadge("i-state", "IMPORTING");
  document.getElementById("i-import").disabled = true;
  try {
    const out = await upload("/api/monthly/import", importForm(true), (p) => { bar.value = p; if (p >= 100) setBadge("i-state", "VALIDATING"); });
    const series = out.series_key;
    const cov = await api(`/api/monthly/coverage?store=research&series=${encodeURIComponent(series)}&month=${encodeURIComponent(document.getElementById("i-month").value)}&timeframe=${encodeURIComponent(out.tf)}`, { timeoutMs: 120000 });
    clear(document.getElementById("i-result")).append(
      el("p", { class: `notice ${out.already_imported ? "warn" : "ok"}`, text: out.already_imported ? `Already imported earlier (import #${out.import_id}): nothing was added.` : `Imported ${out.rows_stored} ${out.tf} candles into ${series} (research store), ${out.rows_rejected} rejected, ${out.duplicates_dropped} duplicates dropped; ${out.first} to ${out.last} UTC.` }),
      el("h3", { text: "Coverage after import" }), ...coverageNodes(cov));
    setBadge("i-state", cov.ready ? "READY" : "INCOMPLETE");
    if (!cov.ready) say("Imported, but the month is not ready yet (coverage or warm-up): the data is kept so missing periods can be added later.", "error");
    else say(`Imported: ${cov.month} ${cov.timeframe} is ready (${pct(cov.share)} coverage, warm-up satisfied).`, "ok");
    await Promise.all([loadImports(), loadDefaults().then(fillCoverageSeries)]);
  } catch (err) { setBadge("i-state", "FAILED"); say(err.message, "error"); } finally { bar.hidden = true; }
}

async function loadImports() {
  const out = await api("/api/monthly/imports");
  clear(document.getElementById("i-history")).append(table(["#", "Label", "Series", "TF", "File", "Rows stored", "Rejected", "From", "To", "Imported"],
    out.imports.map((i) => el("tr", {}, [td(i.id), td(i.label), td(i.series), td(i.tf), td(i.file_name), td(i.rows_stored), td(i.rows_rejected),
      td((i.first || "").slice(0, 16)), td((i.last || "").slice(0, 16)), td(fmtTime(i.imported_at, true))])), "No import yet."));
}

// ------------------------------------------------------------------ 2. coverage
function fillCoverageSeries() {
  const sel = clear(document.getElementById("c-series"));
  for (const s of S.defaults.series_list) sel.append(el("option", { value: `${s.store}|${s.key}`, text: s.label }));
  const research = S.defaults.series_list.find((s) => s.store === "research");
  if (research) sel.value = `research|${research.key}`;
}

async function checkCoverage() {
  const [store, series] = document.getElementById("c-series").value.split("|");
  try {
    const cov = await api(`/api/monthly/coverage?store=${encodeURIComponent(store)}&series=${encodeURIComponent(series)}&month=${encodeURIComponent(document.getElementById("c-month").value)}&timeframe=${encodeURIComponent(document.getElementById("c-tf").value)}`, { timeoutMs: 120000 });
    setBadge("c-state", cov.status);
    clear(document.getElementById("c-result")).append(el("p", { class: "small", text: `${cov.series} (${cov.store} store), ${cov.timeframe}: ${cov.status}${cov.ready ? "" : " - runs on this timeframe would be INCOMPLETE until coverage reaches 95 % and the warm-up history exists"}.` }), ...coverageNodes(cov));
  } catch (err) { setBadge("c-state", "FAILED"); say(err.message, "error"); }
}

// ------------------------------------------------------------------ 4-9. percentage risk scenarios (Phase 5.1)
const R = { studyId: null, poll: null, evals: [] };

async function loadBaselines() {
  const out = await api("/api/monthly/evaluations");
  R.evals = out.evaluations;
  const sel = clear(document.getElementById("r-baseline"));
  const ok = out.evaluations.filter((e) => ["COMPLETED", "INCOMPLETE"].includes(e.status) && e.frozen && !e.params.risk_scenario && !e.params.risk_percent_override);
  for (const e of ok) sel.append(el("option", { value: e.id, text: `${e.id}: ${e.month}, ${e.params.series} (${e.params.store}), ${e.status}` }));
  if (!ok.length) sel.append(el("option", { value: "", text: "no finished fixed-lot evaluation yet" }));
  await showBaselineCandidates();
}

async function showBaselineCandidates() {
  const id = document.getElementById("r-baseline").value;
  const box = clear(document.getElementById("r-candidates"));
  if (!id) return;
  const ev = await api(`/api/monthly/evaluations/${id}`);
  if (!ev.candidates.length) { box.append(el("span", { class: "muted", text: "none in this baseline" })); return; }
  for (const c of ev.candidates) {
    box.append(el("label", { class: "small" }, [el("input", { type: "checkbox", value: c.key, class: "r-cand" }), ` ${c.key} (1:${c.research.chosen_rr}) `]));
  }
}

async function createStudy() {
  const baseline = document.getElementById("r-baseline").value;
  if (!baseline) { say("Choose a finished fixed-lot baseline evaluation first.", "error"); return; }
  const candidates = [...document.querySelectorAll(".r-cand:checked")].map((x) => x.value);
  try {
    const out = await api("/api/monthly/risk-studies", { method: "POST", body: { baseline_evaluation: baseline, candidates }, timeoutMs: 120000 });
    say(`Risk study ${out.study.id} created: ${out.study.evaluation_ids.length} scenario evaluations are running.`, "ok");
    await listStudies();
    openStudy(out.study.id);
  } catch (err) { say(err.message, "error"); }
}

async function listStudies() {
  const out = await api("/api/monthly/risk-studies");
  document.getElementById("r-banner").textContent = out.banner;
  clear(document.getElementById("r-studies")).append(table(["Created", "Study", "Month", "Baseline", "Status", ""],
    out.studies.map((s) => el("tr", {}, [td(fmtTime(s.created_at, true)), td(s.id), td(s.month), td(s.baseline_evaluation_id), statusTd(s.status),
      el("td", {}, el("button", { type: "button", class: "btn sm", text: "Open", onclick: () => openStudy(s.id) }))])), "No risk study yet."));
}

function openStudy(id) {
  R.studyId = id;
  clearInterval(R.poll);
  refreshStudy();
  R.poll = setInterval(refreshStudy, 4000);
}

function cellNode(f, onOpen) {
  if (!f || f.status !== "COMPLETED") {
    const c = el("td", { class: "small" }, el("div", { text: f ? f.status : "—" }));
    if (f && f.reason) c.append(el("div", { class: "muted small", text: f.reason.slice(0, 140) }));
    return c;
  }
  const c = el("td", { class: f.net > 0 ? "up" : f.net < 0 ? "down" : "" }, [
    el("div", { class: "small num", text: `$${num(f.ending_balance)} (${f.net > 0 ? "+" : ""}${num(f.net)})` }),
    el("div", { class: "small muted", text: `${f.trades} tr · DD ${num(f.max_drawdown_pct, 1)}% · streak ${f.longest_losing_streak}` }),
    el("div", { class: "small muted", text: f.average_lots !== null && f.average_lots !== undefined ? `avg ${num(f.average_lots, 2)} lots` : "" }),
  ]);
  c.append(el("button", { type: "button", class: "btn", text: "Report", onclick: () => onOpen(f.run_id) }));
  return c;
}

function studyGrid(rows, columns) {
  return table(["Strategy", "TF", ...columns.map((c) => c.label)], rows.map((r) => el("tr", {}, [td(r.strategy), td(r.timeframe),
    ...columns.map((c) => cellNode(r.cells[c.key], openRun))])), "No runs.");
}

async function refreshStudy() {
  if (!R.studyId) return;
  const s = await api(`/api/monthly/risk-studies/${R.studyId}`, { timeoutMs: 60000 });
  document.getElementById("r-study-panel").hidden = false;
  clear(document.getElementById("r-badge")).append(chip(s.study.status));
  document.getElementById("r-summary").textContent = `${s.study.id}: ${s.study.month}; fixed-lot baseline ${s.study.baseline_evaluation_id} (unchanged); scenario evaluations ${s.evaluations.map((e) => `${e.id} = ${e.risk_scenario.risk_percent}% (${e.status})`).join(", ")}; app ${s.study.app_version}.${s.study.error ? ` Error: ${s.study.error}` : ""}`;
  document.getElementById("r-progress").textContent = s.running_job ? `${s.phase || "starting"} — ${s.progress} of ${s.total} runs done` : "";
  const acts = clear(document.getElementById("r-actions"));
  if (s.running_job) acts.append(el("button", { type: "button", class: "btn", text: "Cancel", onclick: async () => { await api(`/api/monthly/risk-studies/${s.study.id}/cancel`, { method: "POST" }); refreshStudy(); } }));
  if (!s.running_job && ["READY", "FAILED"].includes(s.study.status)) acts.append(el("button", { type: "button", class: "btn", text: "Run the study", onclick: async () => { try { await api(`/api/monthly/risk-studies/${s.study.id}/run`, { method: "POST" }); openStudy(s.study.id); } catch (err) { say(err.message, "error"); } } }));
  clear(document.getElementById("r-grid")).append(studyGrid(s.rows, s.columns));
  const keys = [...new Set(s.rows.map((r) => r.strategy))];
  const ckeys = [...new Set(s.candidates.map((r) => r.strategy))];
  clear(document.getElementById("r-tf-links")).append(...[...keys, ...ckeys].map((k) => el("button", { type: "button", class: "btn", text: `${k}: M15 vs M5 vs M1`, onclick: () => openStudyView(k, "timeframes") })));
  clear(document.getElementById("r-sc-links")).append(...[...keys, ...ckeys].map((k) => el("button", { type: "button", class: "btn", text: `${k}: 5 vs 7.5 vs 10 %`, onclick: () => openStudyView(k, "scenarios") })));
  clear(document.getElementById("r-master-links")).append(link(`/api/monthly/risk-studies/${s.study.id}/master.pdf`, "Master PDF"), link(`/api/monthly/risk-studies/${s.study.id}/master.csv`, "Master CSV"),
    ...keys.map((k) => link(`/api/monthly/risk-studies/${s.study.id}/strategies/${k}/scenarios.pdf`, `${k} scenarios PDF`)),
    ...keys.map((k) => link(`/api/monthly/risk-studies/${s.study.id}/strategies/${k}/timeframes.pdf`, `${k} timeframes PDF`)));
  clear(document.getElementById("r-findings")).append(el("p", { class: "notice warn", text: s.separate_accounts_note }), el("ul", { class: "plain" }, s.findings.map((x) => el("li", { text: x }))));
  clear(document.getElementById("r-cand-grid")).append(s.candidates.length ? studyGrid(s.candidates, s.columns) : el("p", { class: "muted small", text: "No candidate was selected for this study." }));
  if (!s.running_job) { clearInterval(R.poll); R.poll = null; }
}

async function openStudyView(key, view) {
  const out = await api(`/api/monthly/risk-studies/${R.studyId}/strategies/${key}/${view}`, { timeoutMs: 60000 });
  const title = view === "scenarios" ? `${out.name}: 5 % vs 7.5 % vs 10 % (and the fixed-lot baseline)` : `${out.name}: M15 vs M5 vs M1 in each scenario`;
  const detail = table(["TF", "Sizing", "Trades", "End balance", "Net", "Max DD %", "Losing streak", "Avg / max lots", "Avg budget", "Max planned risk", "Beyond plan", "Margin rej.", "Min-vol rej."],
    out.rows.flatMap((r) => out.columns.map((c) => { const f = r.cells[c.key]; return el("tr", {}, f.status === "COMPLETED" ? [td(r.timeframe), td(c.label), td(f.trades), td(num(f.ending_balance)), pnl(f.net), td(num(f.max_drawdown_pct, 1)),
      td(`${f.longest_losing_streak} (${num(f.losing_streak_loss)})`), td(`${num(f.average_lots, 2)} / ${num(f.max_lots, 2)}`), td(num(f.average_risk_budget_usd)), td(num(f.max_planned_risk_usd)),
      td(`${f.losses_beyond_plan} (${num(f.losses_beyond_plan_usd)})`), td(f.margin_rejections), td(f.insufficient_volume_rejections)] : [td(r.timeframe), td(c.label), td(f.status), ...Array(10).fill(null).map(() => td(f.reason || "—"))]); })));
  showReport(title, [link(`/api/monthly/risk-studies/${R.studyId}/strategies/${key}/${view}.pdf`, "PDF")], [
    el("p", { class: "notice error", text: out.banner }),
    el("p", { class: "small muted", text: "Every row is a separate hypothetical $1,000 account; nothing is added together." }),
    detail, el("h3", { text: "Findings" }), el("ul", { class: "plain" }, out.findings.map((x) => el("li", { text: x }))),
  ]);
}

// ------------------------------------------------------------------ 10. strategy redesign and robustness (Phase 5.2)
const RB = { id: null, poll: null };
const r3 = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${Number(v).toFixed(3)}`);
const rtd = (v) => td(r3(v), Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "");
const verdictTd = (v) => td(v, v === "consistent" ? "up" : ["fails", "no version"].includes(v) ? "down" : "");

async function listRobustness() {
  const out = await api("/api/monthly/robustness-studies");
  clear(document.getElementById("rb-list")).append(table(["Created", "Study", "Status", "Progress", ""],
    out.studies.map((s) => el("tr", {}, [td(fmtTime(s.created_at, true)), td(s.id), statusTd(s.status), td(`${s.progress} / ${s.total}`),
      el("td", {}, el("button", { type: "button", class: "btn sm", text: "Open", onclick: () => openRobustness(s.id) }))])), "No research study yet."));
  if (out.running && out.running.startsWith("rb_")) openRobustness(out.running);
}

async function createRobustness() {
  try {
    const s = await api("/api/monthly/robustness-studies", { method: "POST", body: {}, timeoutMs: 120000 });
    say(`Research study ${s.id} started: ${s.params.config_count} configurations over ${s.params.windows.length} months.`, "ok");
    await listRobustness();
    openRobustness(s.id);
  } catch (err) { say(err.message, "error"); }
}

function openRobustness(id) {
  RB.id = id;
  clearInterval(RB.poll);
  refreshRobustness();
  RB.poll = setInterval(refreshRobustness, 10000);
}

async function refreshRobustness() {
  if (!RB.id) return;
  const s = await api(`/api/monthly/robustness-studies/${RB.id}`, { timeoutMs: 60000 });
  document.getElementById("rb-progress").textContent = s.running_job || s.status === "RUNNING"
    ? `${s.id}: ${s.phase || "starting"} — ${s.progress} of ${s.total} account runs done` : `${s.id}: ${s.status}${s.error ? ` — ${s.error}` : ""}`;
  const view = clear(document.getElementById("rb-view"));
  const p = s.params;
  view.append(el("p", { class: "small", text: `Windows: ${p.windows.map((w) => `${w.key} ${w.start.slice(0, 10)}→${w.end.slice(0, 10)} (${w.role}${w.partial ? ", partial" : ""})`).join("; ")}. Walk-forward: ${p.folds.map((f) => `${f.test} chosen on ${f.select_on.join("+")}`).join("; ")}. ${p.config_count} configurations; originals: ${Object.entries(p.originals).map(([k, o]) => `${k} = ${o.name}`).join(", ")}.` }));
  if (!s.analysis) { if (!s.running_job && s.status !== "RUNNING") { clearInterval(RB.poll); RB.poll = null; } return; }
  clearInterval(RB.poll); RB.poll = null;
  const a = s.analysis;
  if (a.stage === "diagnostic") {
    view.append(el("div", { class: "toolbar" }, [link(`/api/monthly/robustness-studies/${s.id}/report.pdf`, "Diagnostics PDF"),
      link(`/api/monthly/robustness-studies/${s.id}/results.csv`, "All results CSV")]),
    el("p", { class: "notice warn", text: "Stage 1 (diagnostic): development months only — why the originals lose and what each exit / stop-management change does. The redesign is built from these findings." }),
    table(["Experiment", "Change", "Strategies", "Improved", "Median change (R/trade)", "Median win-rate change"], a.exit_summary.map((x) => el("tr", {}, [td(x.experiment), td(x.label), td(x.strategies), td(x.improved), rtd(x.median_delta_r), td(x.median_delta_win_rate === null ? "—" : `${(x.median_delta_win_rate * 100).toFixed(1)} pts`)]))),
    ...Object.entries(a.strategies).map(([key, st]) => el("details", {}, [el("summary", { text: `${key} (${st.family}): ${st.weakness.trades} development trades, ${r3(st.weakness.expectancy_r)} R/trade` }),
      el("ul", { class: "plain small" }, st.weakness.findings.map((x) => el("li", { text: x }))),
      table(["Exit (entry unchanged)", "Trades", "Win rate", "R/trade", "Change", "PF"], [el("tr", {}, [td("original"), td(a.exit_experiments[key].original.trades), td(pct(a.exit_experiments[key].original.win_rate)), rtd(a.exit_experiments[key].original.expectancy_r), td("—"), td(num(a.exit_experiments[key].original.profit_factor, 2))]),
        ...a.exit_experiments[key].rows.map((r) => el("tr", {}, [td(`${r.experiment}: ${r.label}`), td(r.trades), td(pct(r.win_rate)), rtd(r.expectancy_r), rtd(r.delta_expectancy_r), td(num(r.profit_factor, 2))]))])])));
    return;
  }
  view.append(el("div", { class: "toolbar" }, [link(`/api/monthly/robustness-studies/${s.id}/report.pdf`, "Research report PDF"),
    link(`/api/monthly/robustness-studies/${s.id}/results.csv`, "All results CSV"), link(`/api/monthly/robustness-studies/${s.id}/configs`, "All configurations (JSON)")]),
  el("ul", { class: "plain small" }, a.principles.map((x) => el("li", { text: x }))),
  el("h3", { text: "Originals vs redesigned research versions" }),
  table(["Strategy", "Family", "Original verdict", "Original OOS R/trade (n)", "Redesigned verdict", "Redesign OOS R/trade (n)", "Redesign hold-out R/trade (n)", "Chosen variant"],
    a.overview.map((o) => el("tr", {}, [td(o.strategy), td(o.family), verdictTd(o.original_verdict), td(`${r3(o.original_oos_expectancy_r)} (${o.original_oos_trades})`),
      verdictTd(o.new_verdict), td(`${r3(o.new_oos_expectancy_r)} (${o.new_oos_trades})`), td(`${r3(o.new_hold_out_expectancy_r)} (${o.new_hold_out_trades ?? "—"})`), td(`${o.chosen || "—"}${o.flag ? ` [${o.flag}]` : ""}`)]))),
  el("h3", { text: "Exit experiments across all originals (development months, M5)" }),
  table(["Experiment", "Change", "Strategies", "Improved", "Median change (R/trade)"], a.exit_summary.map((x) => el("tr", {}, [td(x.experiment), td(x.label), td(x.strategies), td(x.improved), rtd(x.median_delta_r)]))),
  el("h3", { text: "Per strategy: weaknesses, exits, redesign, walk-forward and hold-out" }),
  ...Object.entries(a.strategies).map(([key, st]) => {
    const ex = a.exit_experiments[key];
    const dev = st.development.M5 || {};
    return el("details", {}, [el("summary", { text: `${key} → ${st.version_name}: ${st.verdict.verdict} (original: ${st.original.verdict.verdict})` }),
      el("p", { class: "small", text: "Weakness study (development months, original rules):" }),
      el("ul", { class: "plain small" }, st.weakness.findings.map((x) => el("li", { text: x }))),
      table(["Exit", "Trades", "R/trade", "Change", "PF", "Max DD (R)", "Months better"], [el("tr", {}, [td("original"), td(ex.original.trades), rtd(ex.original.expectancy_r), td("—"), td(num(ex.original.profit_factor, 2)), td(num(ex.original.max_dd_r)), td("—")]),
        ...ex.rows.map((r) => el("tr", {}, [td(`${r.experiment}: ${r.label}`), td(r.trades), rtd(r.expectancy_r), rtd(r.delta_expectancy_r), td(num(r.profit_factor, 2)), td(num(r.max_dd_r)), td(r.windows_improved)]))]),
      el("p", { class: "small", text: `Chosen on development (${st.variants} variants): ${st.selection.chosen || "none"} — ${st.chosen_label || ""}${st.selection.flag ? ` [${st.selection.flag}]` : ""}. Development M5: ${dev.trades ?? "—"} trades, ${r3(dev.expectancy_r)} R/trade, PF ${num(dev.profit_factor, 2)}.` }),
      table(["Fold", "Chosen on", "Chosen", "Test trades", "Test R/trade"], st.walk_forward.map((f) => el("tr", {}, [td(f.test), td(f.selected_on.join("+")), td(f.chosen), td(f.test_metrics ? f.test_metrics.trades : f.status), rtd(f.test_metrics ? f.test_metrics.expectancy_r : null)]))),
      el("p", { class: "small", text: `Walk-forward out-of-sample: ${st.walk_forward_oos.trades} trades, ${r3(st.walk_forward_oos.expectancy_r)} R/trade, PF ${num(st.walk_forward_oos.profit_factor, 2)}, ${st.walk_forward_oos.positive_windows} positive months. Hold-out: ${Object.entries(st.hold_out).map(([tf, m]) => `${tf} ${m.trades} trades ${r3(m.expectancy_r)} R`).join("; ") || "—"}. All variants on the hold-out: ${st.hold_out_all_variants.positive} of ${st.hold_out_all_variants.variants_with_10_trades} positive (median ${r3(st.hold_out_all_variants.median_expectancy_r)} R).` }),
      el("p", { class: "small", text: `Verdict: ${st.verdict.verdict} — ${st.verdict.why}. Original: ${st.original.verdict.verdict} — ${st.original.verdict.why}.` })]);
  }));
}

// ------------------------------------------------------------------ views (sidebar steps)
const VIEWS = ["data", "evaluate", "risk", "research", "library", "candidates"];

function showView(name) {
  const view = VIEWS.includes(name) ? name : "data";
  for (const v of VIEWS) document.getElementById(`view-${v}`).hidden = v !== view;
  for (const a of document.querySelectorAll(".side-nav a[data-view]")) a.setAttribute("aria-current", String(a.dataset.view === view));
  closeReport();
  if (location.hash.slice(1) !== view) history.replaceState(null, "", `#${view}`);
  if (view === "library") {                          // loaded on first visit only; read-only
    import("./library.js").then((m) => m.openLibrary()).catch((e) => say(e.message, "error"));
  }
  if (view === "candidates") {                       // read-only registry of research candidates
    import("./candidates.js").then((m) => m.openCandidates()).catch((e) => say(e.message, "error"));
  }
}

// ------------------------------------------------------------------ boot
async function boot() {
  showView(location.hash.slice(1));
  window.addEventListener("hashchange", () => { showView(location.hash.slice(1)); window.scrollTo({ top: 0 }); });
  document.getElementById("m-report-close").addEventListener("click", closeReport);
  try {
    const cfg = await api("/api/ui-config");
    setDisplayZone(cfg.display_timezone);
    for (const a of document.querySelectorAll("a[data-main]")) a.href = new URL(a.dataset.main, cfg.main_app).href;
  } catch (_) { /* defaults */ }
  document.getElementById("m-check").addEventListener("click", () => checkAvailability().catch((e) => say(e.message, "error")));
  document.getElementById("m-create").addEventListener("click", createEvaluation);
  document.getElementById("i-preview").addEventListener("click", previewImport);
  document.getElementById("i-import").addEventListener("click", runImport);
  document.getElementById("i-file").addEventListener("change", () => { document.getElementById("i-import").disabled = true; setBadge("i-state", "NOT STARTED"); });
  document.getElementById("c-check").addEventListener("click", checkCoverage);
  document.getElementById("r-baseline").addEventListener("change", () => showBaselineCandidates().catch((e) => say(e.message, "error")));
  document.getElementById("r-create").addEventListener("click", createStudy);
  document.getElementById("rb-create").addEventListener("click", createRobustness);
  try { await loadDefaults(); fillCoverageSeries(); await checkAvailability(); } catch (err) { say(err.message, "error"); }
  await Promise.all([listEvaluations(), loadImports(), loadBaselines(), listStudies(), listRobustness()]).catch((e) => say(e.message, "error"));
}

boot();
