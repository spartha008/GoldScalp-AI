// Strategy Lab: Strategy Builder, Backtesting Lab, Performance dashboard, Walk-Forward Lab,
// Strategy Comparison and the Simulated Trade Journal. Hypothetical research only.
import { api } from "./api.js";
import { createMultiLineChart } from "./chart.js";
import { announce, clear, el, fmtPrice, fmtTime, setDisplayZone, setText, storageGet, storageSet } from "./ui.js";

const COLORS = ["#d4af37", "#60a5fa", "#a78bfa", "#2bb594", "#f472b6", "#e0a43a"];
const S = { catalogue: null, strategies: [], series: [], defaults: null, editing: null, conditions: [],
            charts: {}, polls: {}, selected: { results: null, walk: null, compare: null } };
const num = (v, d = 2) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
const toLocal = (iso) => (iso ? iso.slice(0, 16) : "");
const fromLocal = (v) => (v ? `${v}:00Z` : null);

function table(headers, rows, empty = "none") {
  if (!rows.length) return el("p", { class: "muted small", text: empty });
  return el("div", { class: "table-wrap" }, el("table", { class: "compact" }, [
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, rows),
  ]));
}
const stat = (label, value, cls = "") => el("div", { class: "stat" }, [el("div", { class: "label", text: label }),
  el("div", { class: `value num ${cls}`, text: value })]);
const tone = (v) => (v > 0 ? "trend-up" : v < 0 ? "trend-down" : "");

// ------------------------------------------------------------------ tabs
function showTab(name) {
  for (const b of document.querySelectorAll(".lab-tabs [role=tab]")) {
    const on = b.dataset.tab === name;
    b.setAttribute("aria-selected", String(on));
    b.setAttribute("aria-pressed", String(on));
    b.tabIndex = on ? 0 : -1;
    document.getElementById(`panel-${b.dataset.tab}`).hidden = !on;
  }
  storageSet("goldscalp:lab-tab:v1", name);
  if (name === "journal") refreshJournal();
}

// ------------------------------------------------------------------ shared run controls
function commonControls() {
  for (const box of document.querySelectorAll(".run-common")) {
    const p = box.dataset.prefix;
    const sel = el("select", { id: `${p}-series`, required: true });
    for (const s of S.series) sel.append(el("option", { value: s.key, text: `${s.key} (${Object.entries(s.timeframes || {}).map(([tf, t]) => `${tf} ${t.first.slice(0, 10)}→${t.last.slice(0, 10)}`).join(", ")})` }));
    const a = S.defaults.assumptions;
    // data and period stay visible; costs and execution assumptions (pre-filled defaults) fold away
    const costs = el("details", { class: "advanced" }, [
      el("summary", { text: `Costs and execution assumptions — spread ${a.configured_spread}, commission ${a.commission_per_lot_per_side}/lot/side, slippage ${a.slippage}, stop first` }),
      el("div", { class: "form-grid" }, [
        el("label", {}, ["Spread when the data has none (USD)", el("input", { type: "text", id: `${p}-spread`, value: a.configured_spread, inputmode: "decimal" })]),
        el("label", {}, ["Commission per lot per side (USD)", el("input", { type: "text", id: `${p}-comm`, value: a.commission_per_lot_per_side, inputmode: "decimal" })]),
        el("label", {}, ["Adverse slippage per fill (USD/oz)", el("input", { type: "text", id: `${p}-slip`, value: a.slippage, inputmode: "decimal" })]),
        el("label", {}, ["Intrabar rule (stop and target in one candle)", (() => {
          const s = el("select", { id: `${p}-intrabar` });
          s.append(el("option", { value: "stop_first", text: "stop first (conservative)" }), el("option", { value: "target_first", text: "target first (sensitivity only)" }));
          return s;
        })()]),
        el("label", {}, ["Starting balance (USD)", el("input", { type: "text", id: `${p}-balance`, value: a.starting_balance, inputmode: "decimal" })]),
        el("label", {}, ["Minimum trades before ratios are shown", el("input", { type: "number", id: `${p}-mintrades`, value: String(a.min_trades_for_ratios), min: "1", max: "10000" })]),
      ]),
    ]);
    box.replaceWith(
      el("label", {}, ["Dataset", sel]),
      el("label", {}, ["Start (UTC)", el("input", { type: "datetime-local", id: `${p}-start`, step: "60", required: true })]),
      el("label", {}, ["End (UTC)", el("input", { type: "datetime-local", id: `${p}-end`, step: "60", required: true })]),
      el("label", {}, ["History before start (days)", el("input", { type: "number", id: `${p}-lookback`, value: "5", min: "1", max: "20" })]),
      costs,
    );
    const fill = () => {
      const s = S.series.find((x) => x.key === document.getElementById(`${p}-series`).value);
      const tfs = (s && s.timeframes) || {};
      const spans = Object.values(tfs);
      if (!spans.length) return;
      const base = tfs.M5 || tfs.M1 || tfs.M15 || spans[0];       // step strategies need M5 (or finer) candles
      const first = base.first;
      const last = spans.map((t) => t.last).sort().slice(-1)[0];
      document.getElementById(`${p}-start`).value = toLocal(first);
      document.getElementById(`${p}-end`).value = toLocal(new Date(Date.parse(last) + 60000).toISOString());
    };
    const largest = [...S.series].sort((x, y) => total(y) - total(x))[0];
    if (largest) sel.value = largest.key;
    sel.addEventListener("change", fill);
    fill();
  }
}
const total = (s) => Object.values(s.timeframes || {}).reduce((n, t) => n + t.bars, 0);

function runBody(p, strategies) {
  return {
    strategies, series: document.getElementById(`${p}-series`).value,
    start: fromLocal(document.getElementById(`${p}-start`).value), end: fromLocal(document.getElementById(`${p}-end`).value),
    lookback_days: Number(document.getElementById(`${p}-lookback`).value) || 5,
    assumptions: {
      configured_spread: document.getElementById(`${p}-spread`).value, commission_per_lot_per_side: document.getElementById(`${p}-comm`).value,
      slippage: document.getElementById(`${p}-slip`).value, intrabar: document.getElementById(`${p}-intrabar`).value,
      starting_balance: document.getElementById(`${p}-balance`).value, min_trades_for_ratios: Number(document.getElementById(`${p}-mintrades`).value) || 30,
    },
  };
}

// ------------------------------------------------------------------ strategy builder
function tfOptions(select, value) {
  clear(select);
  for (const tf of S.catalogue.timeframes) select.append(el("option", { value: tf, text: tf, selected: tf === value }));
}

function conditionCard(c, index) {
  const spec = S.catalogue.conditions[c.type];
  const fields = [];
  for (const [name, p] of Object.entries(spec.params)) {
    const id = `cond-${index}-${name}`;
    let input;
    if (p.kind === "tf" || p.kind === "choice") {
      input = el("select", { id });
      for (const v of p.kind === "tf" ? S.catalogue.timeframes : p.choices) input.append(el("option", { value: v, text: v, selected: c[name] === v }));
    } else if (p.kind === "bool") {
      input = el("input", { type: "checkbox", id, checked: Boolean(c[name]) });
    } else if (p.kind === "tfs" || p.kind === "choices") {
      input = el("span", { class: "multi", id }, (p.choices || []).map((v) => el("label", { class: "check" }, [
        el("input", { type: "checkbox", value: v, checked: (c[name] || []).includes(v) }), ` ${v}`])));
    } else {
      input = el("input", { type: "number", id, value: c[name] === null || c[name] === undefined ? "" : String(c[name]),
                            step: p.kind === "int" ? "1" : "any", min: p.range ? String(p.range[0]) : null, max: p.range ? String(p.range[1]) : null });
    }
    fields.push(el("label", { class: "small" }, [name.replace(/_/g, " "), input]));
  }
  return el("div", { class: `cond-card${c.enabled ? "" : " disabled"}`, "data-index": String(index) }, [
    el("div", { class: "cond-head" }, [
      el("label", { class: "check" }, [el("input", { type: "checkbox", class: "cond-enabled", checked: c.enabled }), ` ${spec.label}`]),
      el("span", { class: "muted small", text: `id ${c.id}` }), el("span", { class: "spacer" }),
      el("button", { type: "button", class: "btn", text: "Remove", "aria-label": `Remove condition ${c.id}`, onclick: () => { readConditions(); S.conditions.splice(index, 1); renderConditions(); } }),
    ]),
    el("p", { class: "muted small", text: spec.help }),
    el("div", { class: "cond-fields" }, fields),
  ]);
}

function renderConditions() {
  const box = clear(document.getElementById("conditions"));
  if (!S.conditions.length) box.append(el("p", { class: "muted small", text: "No conditions yet: add at least one." }));
  S.conditions.forEach((c, i) => box.append(conditionCard(c, i)));
}

function readConditions() {
  S.conditions = S.conditions.map((c, i) => {
    const card = document.querySelector(`.cond-card[data-index="${i}"]`);
    if (!card) return c;
    const spec = S.catalogue.conditions[c.type];
    const out = { id: c.id, type: c.type, enabled: card.querySelector(".cond-enabled").checked };
    for (const [name, p] of Object.entries(spec.params)) {
      const node = document.getElementById(`cond-${i}-${name}`);
      if (p.kind === "bool") out[name] = node.checked;
      else if (p.kind === "tfs" || p.kind === "choices") out[name] = [...node.querySelectorAll("input:checked")].map((x) => x.value);
      else if (p.kind === "int") out[name] = node.value === "" ? p.default : Number(node.value);
      else if (p.kind === "numopt") out[name] = node.value === "" ? null : Number(node.value);
      else out[name] = node.value;
    }
    return out;
  });
}

function setEditor(record) {
  const c = record ? record.config : null;
  S.editing = record;
  const cfg = c || { name: "", description: "", direction: "long", evaluation: { step_timeframe: "M5", window: { mode: "all", days: 10 } },
    conditions: [], entry: { trigger: "on_change", max_entry_delay_minutes: 10 },
    exit: { stop: { type: "structure", timeframe: "M5", buffer_atr: 0.1, atr_multiple: 1.5, distance: null },
            target: { type: "r_multiple", r: 2, atr_multiple: null, distance: null, timeframe: "M5" }, max_holding_minutes: 60,
            exit_on_structure_flip: { enabled: false, timeframe: "M5" } },
    risk: { sizing: "fixed_lots", lots: "0.10", risk_percent: 0.5, max_open_positions: 1, max_exposure_lots: "1.00" } };
  const v = (id, value) => { document.getElementById(id).value = value === null || value === undefined ? "" : String(value); };
  v("ed-name", cfg.name); v("ed-description", cfg.description); v("ed-direction", cfg.direction); v("ed-step", cfg.evaluation.step_timeframe);
  v("ed-trigger", cfg.entry.trigger); v("ed-delay", cfg.entry.max_entry_delay_minutes);
  v("ed-window-mode", cfg.evaluation.window.mode); v("ed-window-days", cfg.evaluation.window.days);
  v("ed-window-start", toLocal(cfg.evaluation.window.start)); v("ed-window-end", toLocal(cfg.evaluation.window.end));
  const e = cfg.exit;
  v("ed-stop-type", e.stop.type); tfOptions(document.getElementById("ed-stop-tf"), e.stop.timeframe); v("ed-stop-buffer", e.stop.buffer_atr);
  v("ed-stop-atr", e.stop.atr_multiple); v("ed-stop-dist", e.stop.distance);
  v("ed-target-type", e.target.type); v("ed-target-r", e.target.r); v("ed-target-atr", e.target.atr_multiple); v("ed-target-dist", e.target.distance);
  tfOptions(document.getElementById("ed-target-tf"), e.target.timeframe); v("ed-hold", e.max_holding_minutes);
  document.getElementById("ed-flip").checked = e.exit_on_structure_flip.enabled; tfOptions(document.getElementById("ed-flip-tf"), e.exit_on_structure_flip.timeframe);
  v("ed-sizing", cfg.risk.sizing); v("ed-lots", cfg.risk.lots); v("ed-risk", cfg.risk.risk_percent); v("ed-maxpos", cfg.risk.max_open_positions); v("ed-maxexp", cfg.risk.max_exposure_lots);
  v("ed-note", "");
  S.conditions = JSON.parse(JSON.stringify(cfg.conditions));
  renderConditions();
  const saved = Boolean(record && record.id);
  if (!saved) S.editing = null;
  setText("ed-version", saved ? `#${record.id} · version ${record.version} · ${record.fingerprint.slice(0, 12)}` : (record ? "new from template (unsaved)" : "new (unsaved)"));
  const exp = document.getElementById("ed-export");
  exp.hidden = !saved; document.getElementById("ed-archive").hidden = !saved;
  if (saved) { exp.href = `/api/strategies/${record.id}/export.json`; exp.setAttribute("download", ""); }
  announce(document.getElementById("ed-result"), "");
  if (saved) refreshVersions(record.id); else clear(document.getElementById("ed-versions"));
}

function readEditor() {
  readConditions();
  const g = (id) => document.getElementById(id).value;
  const n = (id) => (g(id) === "" ? null : Number(g(id)));
  const mode = g("ed-window-mode");
  return {
    schema: "strategy@1", name: g("ed-name"), description: g("ed-description"), direction: g("ed-direction"),
    evaluation: { step_timeframe: g("ed-step"), window: { mode, days: n("ed-window-days") || 10,
      start: mode === "range" ? fromLocal(g("ed-window-start")) : null, end: mode === "range" ? fromLocal(g("ed-window-end")) : null } },
    conditions: S.conditions,
    entry: { trigger: g("ed-trigger"), max_entry_delay_minutes: n("ed-delay") || 10 },
    exit: { stop: { type: g("ed-stop-type"), timeframe: g("ed-stop-tf"), buffer_atr: n("ed-stop-buffer") ?? 0.1, atr_multiple: n("ed-stop-atr") ?? 1.5, distance: n("ed-stop-dist") },
            target: { type: g("ed-target-type"), r: n("ed-target-r") ?? 2, atr_multiple: n("ed-target-atr"), distance: n("ed-target-dist"), timeframe: g("ed-target-tf") },
            max_holding_minutes: n("ed-hold") ?? 0,
            exit_on_structure_flip: { enabled: document.getElementById("ed-flip").checked, timeframe: g("ed-flip-tf") } },
    risk: { sizing: g("ed-sizing"), lots: g("ed-lots") || "0.10", risk_percent: n("ed-risk") ?? 0.5, max_open_positions: n("ed-maxpos") || 1, max_exposure_lots: g("ed-maxexp") || "1.00" },
  };
}

async function validateEditor() {
  const out = document.getElementById("ed-result");
  try {
    const res = await api("/api/strategies/validate", { method: "POST", body: readEditor() });
    clear(out); out.className = "notice ok"; out.hidden = false;
    out.append(el("strong", { text: `Valid. Logic fingerprint ${res.fingerprint.slice(0, 12)}; timeframes used: ${res.timeframes.join(", ")}.` }),
      el("ul", { class: "small" }, res.conditions_text.map((t) => el("li", { text: t }))));
  } catch (err) { announce(out, `Not valid: ${err.message}`, "error"); }
}

async function saveEditor(event) {
  event.preventDefault();
  const out = document.getElementById("ed-result");
  const body = { ...readEditor(), note: document.getElementById("ed-note").value || null };
  try {
    const rec = S.editing ? await api(`/api/strategies/${S.editing.id}`, { method: "PUT", body })
      : await api("/api/strategies", { method: "POST", body });
    await refreshStrategies();
    setEditor(rec);
    announce(out, S.editing && rec.created_version === false ? "No change: the saved version is identical." : `Saved as version ${rec.version}.`, "ok");
  } catch (err) { announce(out, `Not saved: ${err.message}`, "error"); }
}

async function refreshVersions(id) {
  try {
    const res = await api(`/api/strategies/${id}/versions`);
    const box = clear(document.getElementById("ed-versions"));
    box.append(table(["Version", "Fingerprint", "Created", "Note", ""], res.versions.map((v) => el("tr", {}, [
      el("td", { class: "num", text: v.version }), el("td", { class: "num", text: v.fingerprint.slice(0, 12) }),
      el("td", { class: "num", text: fmtTime(v.created_at, true) }), el("td", { class: "small", text: v.note || "" }),
      el("td", {}, el("button", { type: "button", class: "btn", text: "Load", onclick: async () => setEditor(await api(`/api/strategies/${id}?version=${v.version}`)) })),
    ]))));
  } catch (_) { /* optional */ }
}

async function refreshStrategies() {
  const res = await api("/api/strategies");
  S.strategies = res.strategies;
  const list = clear(document.getElementById("strategy-list"));
  list.append(table(["Strategy", "Version", "Step", "Direction", ""], S.strategies.map((s) => el("tr", { class: S.editing && S.editing.id === s.id ? "here" : "" }, [
    el("td", { text: s.name }), el("td", { class: "num", text: s.version }), el("td", { text: s.config.evaluation.step_timeframe }),
    el("td", { text: s.config.direction }),
    el("td", {}, el("button", { type: "button", class: "btn", text: "Edit", onclick: () => { setEditor(s); refreshStrategies(); } })),
  ])), "No saved strategies yet. Start from a template or a blank strategy."));
  for (const id of ["bt-strategy", "mon-strategy"]) {
    const sel = document.getElementById(id);
    const keep = sel.value;
    clear(sel);
    for (const s of S.strategies) sel.append(el("option", { value: String(s.id), text: `${s.name} (v${s.version})` }));
    if (keep && S.strategies.some((s) => String(s.id) === keep)) sel.value = keep;
  }
  for (const [id, type] of [["cp-strategies", "checkbox"], ["wf-strategies", "checkbox"]]) {
    const box = clear(document.getElementById(id));
    for (const s of S.strategies) box.append(el("label", { class: "check" }, [el("input", { type, value: String(s.id) }), ` ${s.name} (v${s.version})`]));
    if (!S.strategies.length) box.append(el("p", { class: "muted small", text: "Save strategies in the Strategy Builder first." }));
  }
}

// ------------------------------------------------------------------ runs (backtest / comparison / walk-forward)
async function startRun(path, body, statusId, progressId, onDone) {
  const status = document.getElementById(statusId);
  try {
    const res = await api(path, { method: "POST", body, timeoutMs: 120000 });
    announce(status, `Run ${res.run.id} started: ${res.run.total} steps.`, "ok");
    pollRun(res.run.id, statusId, progressId, onDone);
  } catch (err) { announce(status, `Not started: ${err.message}`, "error"); }
}

function pollRun(id, statusId, progressId, onDone) {
  clearTimeout(S.polls[statusId]);
  const tick = async () => {
    let run;
    try { run = (await api(`/api/research/runs/${id}`)).run; } catch (err) { announce(document.getElementById(statusId), err.message, "error"); return; }
    const bar = document.getElementById(progressId);
    bar.style.width = `${run.total ? Math.round((run.progress / run.total) * 100) : 0}%`;
    if (run.status === "queued" || run.status === "running") {
      setText(statusId, `Evaluating ${run.progress} of ${run.total} steps… (the feature timeline is computed once and shared by every strategy)`);
      S.polls[statusId] = setTimeout(tick, 1500);
      return;
    }
    refreshRuns();
    if (run.status === "complete") { announce(document.getElementById(statusId), "Complete.", "ok"); onDone(run); }
    else announce(document.getElementById(statusId), `Run ${run.status}: ${run.error || ""}`, run.status === "cancelled" ? "warn" : "error");
  };
  tick();
}

async function refreshRuns() {
  const saved = document.getElementById("runs-saved").checked;
  let res;
  try { res = await api(`/api/research/runs${saved ? "?saved=true" : ""}`); } catch (_) { return; }
  setText("bt-busy", res.busy ? `running: ${res.busy}` : "");
  const tones = { complete: "ok", running: "run", queued: "run", failed: "err", cancelled: "warn" };
  const rows = res.runs.map((r) => {
    const names = (r.params.strategies || []).map((s) => `${s.name} v${s.version}`);
    return el("tr", {}, [
    el("td", { class: "num", text: fmtTime(r.created_at, true) }), el("td", { text: r.kind.replace("_", " ") }),
    el("td", { text: r.name || "—" }),
    el("td", { class: "small", title: names.join("\n") }, el("span", { class: "truncate", text: names.length > 1 ? `${names.length} strategies: ${names.join("; ")}` : names.join("") || "—" })),
    el("td", { class: "small num", text: `${(r.params.start || "").slice(0, 16)} → ${(r.params.end || "").slice(0, 16)}` }),
    el("td", {}, el("span", { class: `chip ${tones[r.status] || "info"}`, text: r.status === "running" ? `running ${r.progress}/${r.total}` : r.status })),
    el("td", { class: "actions" }, [
      el("button", { type: "button", class: "btn", text: "Open", disabled: r.status !== "complete", onclick: () => openRun(r) }), " ",
      el("button", { type: "button", class: "btn", text: r.saved ? "Unsave" : "Save report", disabled: r.status !== "complete",
                     onclick: async () => { await api(`/api/research/runs/${r.id}`, { method: "PATCH", body: { saved: !r.saved, name: r.name || prompt("Report name", r.name || "") || r.name } }); refreshRuns(); } }), " ",
      r.status === "running" ? el("button", { type: "button", class: "btn", text: "Cancel", onclick: () => api(`/api/research/runs/${r.id}/cancel`, { method: "POST" }).then(refreshRuns) })
        : el("button", { type: "button", class: "btn", text: "Delete", "aria-label": `Delete run ${r.id}`, onclick: () => api(`/api/research/runs/${r.id}`, { method: "DELETE" }).then(refreshRuns) }),
    ]),
  ]);
  });
  const t = table(["Created", "Kind", "Name", "Strategies", "Range (UTC)", "Status", ""], rows, "No research runs yet.");
  const tbl = t.querySelector("table");
  if (tbl) tbl.classList.add("data");
  clear(document.getElementById("runs-body")).append(t);
}

async function openRun(r) {
  const run = (await api(`/api/research/runs/${r.id}`)).run;
  if (run.kind === "walk_forward") { renderWalk(run); showTab("walk"); }
  else if (run.kind === "comparison") { renderCompare(run); renderResults(run); showTab("compare"); }
  else { renderResults(run); showTab("results"); }
}

function exportLinks(run) {
  return el("div", { class: "toolbar" }, [
    el("a", { class: "btn", href: `/api/research/runs/${run.id}/trades.csv`, download: "", text: "Trades CSV" }),
    el("a", { class: "btn", href: `/api/research/runs/${run.id}/summary.csv`, download: "", text: "Summary CSV" }),
    el("a", { class: "btn", href: `/api/research/runs/${run.id}/report.pdf`, download: "", text: "PDF report" }),
  ]);
}

function runHead(run) {
  const r = run.result;
  const cov = r.coverage || {};
  return [
    el("p", { class: "small", text: `${r.series} · ${fmtTime(r.range.start, true)} → ${fmtTime(r.range.end, true)} · base ${r.base_tf} (${cov.base_choice || ""}) · `
      + `${cov.base_candles_in_range ?? "?"} candles${cov.gaps ? `, ${cov.gaps.missing} missing of ${cov.gaps.expected}` : ""}` }),
    el("p", { class: "muted small", text: `Assumptions: spread ${r.assumptions.configured_spread} when the data has none, commission ${r.assumptions.commission_per_lot_per_side}/lot/side, slippage ${r.assumptions.slippage}/fill, intrabar ${r.assumptions.intrabar}, balance ${r.assumptions.starting_balance}, ratios from ${r.assumptions.min_trades_for_ratios} trades.` }),
    exportLinks(run),
    el("details", { class: "small" }, [el("summary", { text: "Limitations" }), el("ul", {}, (r.limitations || []).map((x) => el("li", { text: x })))]),
    el("p", { class: "muted small", text: r.disclaimer }),
  ];
}

function chartInto(id, series) {
  if (!S.charts[id]) S.charts[id] = createMultiLineChart(document.getElementById(id));
  S.charts[id].setSeries(series);
}

function metricCards(s) {
  const m = s.metrics.summary;
  const withheld = m.reason ? el("p", { class: "notice warn small", text: m.reason }) : null;
  return el("section", { class: "panel" }, [
    el("header", {}, [el("h2", { text: s.strategy.name }), el("span", { class: "spacer" }), el("span", { class: "muted small", text: `v${s.strategy.version ?? "?"} · ${s.steps} steps` })]),
    el("div", { class: "body" }, [
      withheld,
      el("div", { class: "metric-grid" }, [
        stat("Trades", m.trades), stat("Wins / losses", `${m.wins} / ${m.losses}`), stat("Win rate", m.win_rate === null ? "withheld" : `${pct(m.win_rate)}${m.win_rate_ci95 ? ` (${pct(m.win_rate_ci95[0])}–${pct(m.win_rate_ci95[1])})` : ""}`),
        stat("Net (USD)", fmtPrice(m.net), tone(m.net)), stat("Gross profit / loss", `${fmtPrice(m.gross_profit)} / ${fmtPrice(m.gross_loss)}`),
        stat("Profit factor", m.profit_factor === null ? "withheld" : num(m.profit_factor, 2)), stat("Expectancy (R)", m.expectancy_r === null ? "withheld" : num(m.expectancy_r, 3)),
        stat("Average outcome (USD)", m.average_outcome === null ? "withheld" : fmtPrice(m.average_outcome)),
        stat("Max drawdown", `${fmtPrice(m.max_drawdown)} (${num(m.max_drawdown_pct)}%)`), stat("Max DD (marked)", fmtPrice(m.max_drawdown_marked)),
        stat("MFE / MAE avg (R)", m.mfe_r_avg === null ? "withheld" : `${num(m.mfe_r_avg)} / ${num(m.mae_r_avg)}`),
        stat("Avg holding (min)", m.average_holding_minutes === null ? "withheld" : num(m.average_holding_minutes, 0)),
        stat("Costs (USD)", `spread ${fmtPrice(m.total_spread_cost)} · comm ${fmtPrice(m.total_commission)} · slip ${fmtPrice(m.total_slippage_cost)}`),
        stat("Ambiguous candles", m.ambiguous_bar_trades),
      ]),
      el("p", { class: "muted small", text: `Exits: ${Object.entries(m.exit_reasons).map(([k, v]) => `${k} ${v}`).join(" · ") || "—"}. Setups ${s.diagnostics.setups}, entries ${s.diagnostics.entries}, skipped: ${Object.entries(s.diagnostics.skipped).map(([k, v]) => `${k} ${v}`).join(", ")}; gated steps ${s.diagnostics.gated_steps}.` }),
      el("details", { class: "small" }, [el("summary", { text: "Conditions and how often each passed" }),
        el("ul", {}, (s.conditions_text || []).map((t) => el("li", { text: t }))),
        el("p", { class: "muted", text: Object.entries(s.diagnostics.condition_pass_counts).map(([k, v]) => `${k}: ${v}/${s.steps}`).join(" · ") })]),
    ]),
  ]);
}

function groupTable(rows) {
  return table(["Group", "Trades", "Wins", "Net USD", "Win rate", "PF", "Exp. R"], rows.map((g) => el("tr", {}, [
    el("td", { text: g.group }), el("td", { class: "num", text: g.trades }), el("td", { class: "num", text: g.wins }),
    el("td", { class: `num ${g.net > 0 ? "up" : g.net < 0 ? "down" : ""}`, text: fmtPrice(g.net) }),
    el("td", { class: "num", text: g.win_rate === null ? "withheld" : pct(g.win_rate) }),
    el("td", { class: "num", text: g.profit_factor === null ? "—" : num(g.profit_factor) }),
    el("td", { class: "num", text: g.expectancy_r === null ? "—" : num(g.expectancy_r, 3) }),
  ])));
}

function renderResults(run) {
  const r = run.result;
  setText("rs-run", `${run.kind} ${run.id}${run.name ? ` · ${run.name}` : ""}`);
  clear(document.getElementById("rs-head")).append(...runHead(run));
  clear(document.getElementById("rs-metrics")).append(...r.strategies.map(metricCards));
  chartInto("rs-equity", r.strategies.map((s, i) => ({ name: s.strategy.name, color: COLORS[i % COLORS.length],
    points: s.marked_equity.map((p) => ({ time: Math.floor(Date.parse(p.time) / 1000), value: p.equity })) })));
  chartInto("rs-drawdown", r.strategies.map((s, i) => ({ name: `${s.strategy.name} drawdown`, color: COLORS[i % COLORS.length],
    points: s.metrics.drawdown.map((p) => ({ time: Math.floor(Date.parse(p.time) / 1000), value: p.drawdown })) })));
  const bd = clear(document.getElementById("rs-breakdowns"));
  for (const s of r.strategies) {
    for (const [key, title] of [["by_session", "session"], ["by_volatility", "volatility regime"], ["by_timeframe", "timeframe"], ["by_side", "side"]]) {
      bd.append(el("section", { class: "panel" }, [el("header", {}, el("h2", { text: `${s.strategy.name}: by ${title}` })), el("div", { class: "body" }, groupTable(s.metrics[key]))]));
    }
  }
  const rows = r.strategies.flatMap((s) => s.trades.map((t) => el("tr", {}, [
    el("td", { text: s.strategy.name }), el("td", { class: t.side === "buy" ? "up" : "down", text: t.side }),
    el("td", { class: "num", text: fmtTime(t.setup_cutoff, true) }), el("td", { class: "num", text: fmtTime(t.entry_time, true) }),
    el("td", { class: "num", text: num(t.entry_price) }), el("td", { class: "num", text: num(t.stop) }), el("td", { class: "num", text: num(t.target) }),
    el("td", { class: "num", text: fmtTime(t.exit_time, true) }), el("td", { text: t.exit_reason + (t.ambiguous_bar ? " (ambiguous)" : "") }),
    el("td", { class: `num ${t.net_pnl > 0 ? "up" : t.net_pnl < 0 ? "down" : ""}`, text: fmtPrice(t.net_pnl) }), el("td", { class: "num", text: num(t.r_multiple) }),
    el("td", { class: "num", text: `${num(t.mfe)} / ${num(t.mae)}` }), el("td", { class: "num", text: num(t.holding_minutes, 0) }), el("td", { class: "small", text: t.session }),
  ])));
  clear(document.getElementById("rs-trades")).append(table(["Strategy", "Side", "Signal", "Entry", "Entry px", "Stop", "Target", "Exit", "Reason", "Net", "R", "MFE/MAE", "Min", "Session"], rows, "No hypothetical trades in this run."));
}

function renderCompare(run) {
  const r = run.result;
  setText("cp-run", `${run.id}${run.name ? ` · ${run.name}` : ""}`);
  const box = clear(document.getElementById("cp-result"));
  box.append(...runHead(run));
  const keys = [["Observations (trades)", (c) => c.trades], ["Steps", (c) => c.steps], ["Win rate", (c) => (c.win_rate === null ? "withheld" : pct(c.win_rate))],
    ["Profit factor", (c) => (c.profit_factor === null ? "withheld" : num(c.profit_factor))], ["Expectancy (R)", (c) => (c.expectancy_r === null ? "withheld" : num(c.expectancy_r, 3))],
    ["Net (USD)", (c) => fmtPrice(c.net)], ["Max drawdown", (c) => fmtPrice(c.max_drawdown)], ["MFE avg (R)", (c) => num(c.mfe_r_avg)], ["MAE avg (R)", (c) => num(c.mae_r_avg)],
    ["Avg holding (min)", (c) => num(c.average_holding_minutes, 0)], ["Ambiguous candles", (c) => c.ambiguous_bar_trades]];
  box.append(table(["", ...r.comparison.map((c) => c.strategy.name)], keys.map(([label, f]) => el("tr", {}, [el("th", { scope: "row", text: label }), ...r.comparison.map((c) => el("td", { class: "num", text: f(c) }))]))));
  box.append(el("h3", { text: "Conditions" }));
  for (const c of r.comparison) box.append(el("p", { class: "small" }, [el("strong", { text: `${c.strategy.name}: ` }), c.conditions.join("; ")]));
  for (const key of ["by_session", "by_volatility"]) {
    box.append(el("h3", { text: key === "by_session" ? "Session performance" : "Volatility performance" }));
    const groups = [...new Set(r.strategies.flatMap((s) => s.metrics[key].map((g) => g.group)))].sort();
    box.append(table(["Group", ...r.strategies.map((s) => s.strategy.name)], groups.map((g) => el("tr", {}, [el("th", { scope: "row", text: g }),
      ...r.strategies.map((s) => { const row = s.metrics[key].find((x) => x.group === g); return el("td", { class: "num", text: row ? `${row.trades} tr · ${fmtPrice(row.net)}` : "—" }); })]))));
  }
  if (r.comparison.some((c) => !c.sufficient)) box.append(el("p", { class: "notice warn small", text: "Some strategies have too few trades for ratios: those values are withheld rather than shown misleadingly." }));
  chartInto("cp-equity", r.strategies.map((s, i) => ({ name: s.strategy.name, color: COLORS[i % COLORS.length],
    points: s.marked_equity.map((p) => ({ time: Math.floor(Date.parse(p.time) / 1000), value: p.equity })) })));
  chartInto("cp-drawdown", r.strategies.map((s, i) => ({ name: `${s.strategy.name} drawdown`, color: COLORS[i % COLORS.length],
    points: s.metrics.drawdown.map((p) => ({ time: Math.floor(Date.parse(p.time) / 1000), value: p.drawdown })) })));
}

function renderWalk(run) {
  const r = run.result;
  setText("wf-run", `${run.id}${run.name ? ` · ${run.name}` : ""}`);
  const box = clear(document.getElementById("wf-result"));
  box.append(...runHead(run));
  const o = r.out_of_sample;
  box.append(el("div", { class: "metric-grid" }, [
    stat("Mode / objective", `${r.mode} / ${r.objective}`), stat("Folds traded", `${r.variation.traded_folds} of ${r.variation.folds}`),
    stat("Positive folds", r.variation.positive_folds), stat("Out-of-sample trades", o.trades), stat("Out-of-sample net", fmtPrice(o.net), tone(o.net)),
    stat("Fold net min / median / max", `${fmtPrice(r.variation.min_net)} / ${fmtPrice(r.variation.median_net)} / ${fmtPrice(r.variation.max_net)}`),
    stat("In-sample net (selected)", fmtPrice(r.in_sample_vs_out_of_sample.in_sample_net_total)),
    stat("Win rate (OOS)", o.win_rate === null ? "withheld" : pct(o.win_rate)), stat("Profit factor (OOS)", o.profit_factor === null ? "withheld" : num(o.profit_factor)),
    stat("Max drawdown (OOS)", fmtPrice(o.max_drawdown)),
  ]));
  if (o.reason) box.append(el("p", { class: "notice warn small", text: o.reason }));
  box.append(table(["Fold", "Development", "Evaluation (unseen)", "Candidates (dev trades / net / objective)", "Selected", "OOS trades", "OOS net", "PF"],
    r.folds.map((f) => el("tr", {}, [
      el("td", { class: "num", text: f.fold }), el("td", { class: "small", text: `${f.dev_start.slice(0, 16)} → ${f.dev_end.slice(0, 16)}` }),
      el("td", { class: "small", text: `${f.eval_start.slice(0, 16)} → ${f.eval_end.slice(0, 16)}` }),
      el("td", { class: "small", text: f.candidates.map((c) => `${c.name}: ${c.dev_trades} / ${fmtPrice(c.dev_net)}${c.objective !== undefined ? ` / ${c.objective ?? "n/q"}` : ""}`).join("; ") }),
      el("td", { text: f.selected || f.note || "—" }), el("td", { class: "num", text: f.evaluation ? f.evaluation.trades : "—" }),
      el("td", { class: `num ${f.evaluation && f.evaluation.net > 0 ? "up" : f.evaluation && f.evaluation.net < 0 ? "down" : ""}`, text: f.evaluation ? fmtPrice(f.evaluation.net) : "—" }),
      el("td", { class: "num", text: f.evaluation && f.evaluation.profit_factor !== null ? num(f.evaluation.profit_factor) : "—" }),
    ]))));
  box.append(el("p", { class: "muted small", text: r.note }));
  chartInto("wf-equity", [{ name: "out-of-sample equity", color: COLORS[0], points: r.out_of_sample_equity.map((p) => ({ time: Math.floor(Date.parse(p.time) / 1000), value: p.equity })) },
                          { name: "out-of-sample drawdown", color: "#e5484d", points: r.out_of_sample_drawdown.map((p) => ({ time: Math.floor(Date.parse(p.time) / 1000), value: p.drawdown + Number(r.assumptions.starting_balance) })) }]);
}

// ------------------------------------------------------------------ journal and live monitor
async function checkLive() {
  const id = document.getElementById("mon-strategy").value;
  const box = clear(document.getElementById("mon-result"));
  if (!id) { box.append(el("p", { class: "muted small", text: "Save a strategy first." })); return; }
  let res;
  try { res = await api(`/api/strategies/${id}/live`, { timeoutMs: 30000 }); } catch (err) { box.append(el("p", { class: "notice error", text: err.message })); return; }
  box.append(el("p", { class: "small" }, [el("strong", { text: res.setup ? `Setup: ${res.setup.toUpperCase()}` : "No setup now" }), ` · ${res.series} · market ${res.market_state} · as of ${fmtTime(res.as_of, true)}`]));
  for (const g of res.gates || []) box.append(el("p", { class: "notice warn small", text: g }));
  for (const [side, ev] of Object.entries(res.evaluations || {})) {
    box.append(el("h3", { text: `${side}: ${ev.passed ? "all conditions met" : "not met"}` }));
    if (ev.gate.length) box.append(el("p", { class: "muted small", text: `Data quality: ${ev.gate.join("; ")}` }));
    box.append(el("ul", { class: "evidence" }, ev.conditions.map((c) => el("li", { class: "small" }, [el("span", { class: `pill ${c.passed ? "bull" : "bear"}`, text: c.passed ? "yes" : "no" }), el("span", { class: "rule", text: c.id }), el("span", { class: "obs", text: c.evidence })]))));
  }
  if (res.setup && res.plan) {
    const p = res.plan;
    box.append(el("dl", { class: "kv small" }, [el("dt", { text: "Entry" }), el("dd", { text: p.entry_assumption }), el("dt", { text: "Stop" }), el("dd", { text: `${p.stop} (${p.stop_source})` }),
      el("dt", { text: "Target" }), el("dd", { text: p.target || "none" }), el("dt", { text: "Size" }), el("dd", { text: `${p.lots || "below minimum lot"} lots (${p.sizing})` }),
      el("dt", { text: "Planned max holding" }), el("dd", { text: `${p.max_holding_minutes} min (shown in the journal; close manually)` })]));
    const confirm = el("button", { type: "button", class: "btn primary", text: "Confirm: open SIMULATED paper position", hidden: true, onclick: async () => {
      try { const out = await api(`/api/strategies/${id}/paper`, { method: "POST" }); announce(document.getElementById("lab-status"), `Simulated position #${out.position_id} opened (journal #${out.journal_id}). No broker order was sent.`, "ok"); refreshJournal(); }
      catch (err) { announce(document.getElementById("lab-status"), `Not opened: ${err.message}`, "error"); }
    } });
    box.append(el("div", { class: "toolbar" }, [el("button", { type: "button", class: "btn", text: "Prepare a simulated paper position…", onclick: (e) => { e.target.hidden = true; confirm.hidden = false; } }), confirm]));
  }
  box.append(el("p", { class: "muted small", text: res.note }));
}

async function refreshJournal() {
  let res;
  try { res = await api("/api/journal"); } catch (_) { return; }
  const rows = res.entries.map((e) => el("tr", {}, [
    el("td", { class: "num", text: e.id }), el("td", { text: `${e.strategy_name} v${e.strategy_version}` }), el("td", { class: "num", text: fmtTime(e.signal_time, true) }),
    el("td", { class: e.side === "buy" ? "up" : "down", text: e.side }), el("td", { class: "num", text: `${e.lots} @ ${num(e.entry_price)}` }),
    el("td", { class: "small", text: `stop ${e.planned.stop} · target ${e.planned.target || "none"} · ${e.planned.max_holding_minutes} min` }),
    el("td", { class: "small", text: `spread ${e.entry_costs.spread} · comm ${e.entry_costs.commission} · slip ${e.entry_costs.slippage}` }),
    el("td", { text: e.status + (e.holding_exceeded ? " (holding exceeded)" : "") }),
    el("td", { class: "num", text: e.exit_price ? `${num(e.exit_price)} at ${fmtTime(e.exit_time, true)}` : "—" }),
    el("td", { class: `num ${Number(e.net_pnl) > 0 ? "up" : Number(e.net_pnl) < 0 ? "down" : ""}`, text: e.net_pnl ? fmtPrice(e.net_pnl) : "—" }),
    el("td", { class: "small", text: e.invalidation_reason || e.note || "" }),
  ]));
  clear(document.getElementById("journal-body")).append(
    table(["#", "Strategy", "Signal", "Side", "Entry", "Planned exit", "Entry costs", "Status", "Exit", "Net", "Outcome / invalidation"], rows,
      "No strategy-linked simulated positions yet. Use the strategy monitor above (needs LIVE data)."),
    el("p", { class: "muted small", text: res.note }),
    el("p", { class: "muted small", text: "Hypothetical backtest trades are listed per run on the Performance tab and in each run's Trades CSV." }));
}

// ------------------------------------------------------------------ wiring
async function main() {
  try { setDisplayZone((await api("/api/ui-config")).display_timezone || "UTC"); } catch (_) { /* UTC */ }
  [S.catalogue, S.defaults] = await Promise.all([api("/api/strategies/catalogue"), api("/api/research/defaults")]);
  S.series = (await api("/api/market/series")).data.series;
  setText("lab-disclaimer", S.defaults.disclaimer);
  const step = document.getElementById("ed-step");
  for (const tf of S.catalogue.step_timeframes) step.append(el("option", { value: tf, text: tf }));
  const add = document.getElementById("add-type");
  for (const [k, v] of Object.entries(S.catalogue.conditions)) add.append(el("option", { value: k, text: v.label }));
  const from = document.getElementById("new-from");
  S.catalogue.templates.forEach((t, i) => from.append(el("option", { value: String(i), text: t.name })));
  document.getElementById("new-strategy").addEventListener("click", () => {
    const i = from.value;
    setEditor(null);
    if (i !== "") setEditor({ config: JSON.parse(JSON.stringify(S.catalogue.templates[Number(i)])), id: null });
  });
  document.getElementById("add-condition").addEventListener("click", () => {
    readConditions();
    const type = add.value;
    const spec = S.catalogue.conditions[type];
    const used = new Set(S.conditions.map((c) => c.id));
    let n = S.conditions.length + 1;
    while (used.has(`c${n}`)) n += 1;
    const c = { id: `c${n}`, type, enabled: true };
    for (const [name, p] of Object.entries(spec.params)) c[name] = JSON.parse(JSON.stringify(p.default));
    S.conditions.push(c);
    renderConditions();
  });
  document.getElementById("editor").addEventListener("submit", saveEditor);
  document.getElementById("ed-validate").addEventListener("click", validateEditor);
  document.getElementById("ed-archive").addEventListener("click", async () => {
    if (!S.editing) return;
    await api(`/api/strategies/${S.editing.id}`, { method: "DELETE" });
    setEditor(null); refreshStrategies();
  });
  document.getElementById("import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    try { const rec = await api("/api/strategies/import", { method: "POST", body: JSON.parse(await file.text()) }); await refreshStrategies(); setEditor(rec); announce(document.getElementById("ed-result"), `Imported as strategy #${rec.id}.`, "ok"); }
    catch (err) { announce(document.getElementById("ed-result"), `Import refused: ${err.message}`, "error"); }
    e.target.value = "";
  });
  commonControls();
  document.getElementById("bt-form").addEventListener("submit", (e) => {
    e.preventDefault();
    startRun("/api/research/backtests", runBody("bt", [{ id: Number(document.getElementById("bt-strategy").value) }]), "bt-status", "bt-progress",
      (run) => { renderResults(run); showTab("results"); });
  });
  document.getElementById("cp-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const ids = [...document.querySelectorAll("#cp-strategies input:checked")].map((x) => ({ id: Number(x.value) }));
    startRun("/api/research/comparisons", { ...runBody("cp", ids), name: document.getElementById("cp-name").value || null }, "cp-status", "cp-progress",
      (run) => { renderCompare(run); renderResults(run); });
  });
  document.getElementById("wf-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const ids = [...document.querySelectorAll("#wf-strategies input:checked")].map((x) => ({ id: Number(x.value) }));
    const g = (id) => document.getElementById(id).value;
    startRun("/api/research/walk-forward", { ...runBody("wf", ids), mode: g("wf-mode"), objective: g("wf-objective"), min_dev_trades: Number(g("wf-min")) || 5,
      dev_days: Number(g("wf-dev")), eval_days: Number(g("wf-eval")), step_days: g("wf-step") ? Number(g("wf-step")) : null,
      embargo_minutes: g("wf-embargo") === "" ? null : Number(g("wf-embargo")), anchored: document.getElementById("wf-anchored").checked }, "wf-status", "wf-progress", renderWalk);
  });
  document.getElementById("runs-saved").addEventListener("change", refreshRuns);
  document.getElementById("mon-check").addEventListener("click", checkLive);
  for (const b of document.querySelectorAll(".lab-tabs [role=tab]")) b.addEventListener("click", () => showTab(b.dataset.tab));
  document.querySelector(".lab-tabs").addEventListener("keydown", (e) => {
    const tabs = [...document.querySelectorAll(".lab-tabs [role=tab]")];
    const i = tabs.indexOf(document.activeElement);
    if (i < 0 || !["ArrowRight", "ArrowLeft"].includes(e.key)) return;
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    next.focus(); showTab(next.dataset.tab);
  });
  setEditor(null);
  await refreshStrategies();
  await refreshRuns();
  const hash = window.location.hash.replace("#", "");
  showTab(["builder", "backtest", "results", "walk", "compare", "journal"].includes(hash) ? hash : storageGet("goldscalp:lab-tab:v1", "builder"));
}

main().catch((err) => announce(document.getElementById("lab-status"), `Could not start the Strategy Lab: ${err.message}`, "error"));
