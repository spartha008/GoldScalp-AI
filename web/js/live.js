// Live Validation: Vantage MT5 connection and verification, live strategy signals, SIMULATED validation positions,
// live-paper performance vs backtests, the journal with per-trade evaluations, improvement proposals (approve/reject)
// and the research assistant. Market data is read-only; every trade here is SIMULATED.
import { api } from "./api.js";
import { createMultiLineChart } from "./chart.js";
import { announce, clear, el, fmtPrice, fmtTime, setDisplayZone, stateBadge, storageGet, storageSet } from "./ui.js";

const COLORS = ["#d4af37", "#60a5fa", "#a78bfa", "#2bb594", "#f472b6", "#e0a43a"];
const S = { tab: "overview", strategies: [], chart: null, timer: null, studyPoll: null };
const num = (v, d = 2) => (v === null || v === undefined || v === "" ? "—" : Number(v).toFixed(d));
const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
const statusNode = () => document.getElementById("live-status");
const say = (msg, tone = "") => announce(statusNode(), msg, tone);

function table(headers, rows, empty = "none") {
  if (!rows.length) return el("p", { class: "muted small", text: empty });
  return el("div", { class: "table-wrap" }, el("table", { class: "compact" }, [
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, rows),
  ]));
}
const td = (text, cls = "") => el("td", { class: cls, text: text === null || text === undefined || text === "" ? "—" : String(text) });
const pnl = (v) => td(v === null || v === undefined ? "—" : num(v), Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "");
const stat = (label, value, cls = "") => el("div", { class: "stat" }, [el("div", { class: "label", text: label }),
  el("div", { class: `value num ${cls}`, text: value === null || value === undefined || value === "" ? "—" : String(value) })]);

// ------------------------------------------------------------------ tabs
function showTab(name) {
  S.tab = name;
  for (const b of document.querySelectorAll(".lab-tabs [role=tab]")) {
    const on = b.dataset.tab === name;
    b.setAttribute("aria-selected", String(on));
    b.setAttribute("aria-pressed", String(on));
    b.tabIndex = on ? 0 : -1;
    document.getElementById(`panel-${b.dataset.tab}`).hidden = !on;
  }
  storageSet("goldscalp:live-tab:v1", name);
  refresh();
}

async function refresh() {
  try {
    if (S.tab === "overview") await refreshOverview();
    else if (S.tab === "monitor") await refreshMonitor();
    else if (S.tab === "positions") await refreshPositions();
    else if (S.tab === "demo") await refreshDemo();
    else if (S.tab === "performance") await refreshPerformance();
    else if (S.tab === "journal") await refreshJournal();
    else if (S.tab === "improve") await refreshProposals();
    else if (S.tab === "assistant") await refreshAssistant();
  } catch (err) { say(err.message, "error"); }
}

// ------------------------------------------------------------------ overview
async function refreshOverview() {
  const st = await api("/api/live/status");
  const state = document.getElementById("conn-state");
  clear(state).append(stateBadge(st.state));
  const t = st.terminal || {};
  const id = st.broker_identity || {};
  const env = st.environment || {};
  const ver = st.verification;
  clear(document.getElementById("conn-grid")).append(
    stat("Provider", st.provider),
    stat("Terminal company", t.broker || "—"),
    stat("Vantage broker", id.status ? (id.is_vantage ? `yes (${id.basis})` : id.status === "WARN" ? "unconfirmed" : "no") : "—",
         id.is_vantage ? "trend-up" : id.status === "FAIL" ? "trend-down" : ""),
    stat("Environment", env.environment || "—", env.environment === "DEMO" ? "trend-up" : ""),
    stat("Server", env.server || "—"),
    stat("Terminal build", t.build || "—"),
    stat("Symbol", st.symbol || "—"),
    stat("Monitored", st.monitored_active),
  );
  const summary = document.getElementById("verify-summary");
  if (!ver) summary.textContent = "Not verified: no terminal verification has been run yet.";
  else summary.textContent = `Last verification ${fmtTime(ver.started_at, true)}: ${ver.overall}` +
    (ver.actual_terminal ? " (actual MetaTrader5 terminal)" : ` (adapter ${ver.adapter}: NOT an actual-terminal run)`) +
    (st.verified ? " - verified." : "");
  if (ver && !S.checksShown) {
    const full = await api(`/api/live/verifications/${ver.id}`);
    renderChecks(full);
  }
  const q = st.quote || {};
  const f = st.feed || {};
  clear(document.getElementById("price-grid")).append(
    stat("Bid", fmtPrice(q.bid)), stat("Ask", fmtPrice(q.ask)), stat("Spread", q.spread ? num(q.spread, 2) : "—"),
    stat("Mid", fmtPrice(q.mid)), stat("Tick age", q.age_seconds !== undefined ? `${q.age_seconds} s` : "—"),
    stat("Session", (st.sessions || []).join(" / ") || "none"),
    stat("Spread median 1 h", f.median !== null && f.median !== undefined ? num(f.median, 3) : "—"),
    stat("Spread p95 1 h", f.p95 !== null && f.p95 !== undefined ? num(f.p95, 3) : "—"),
  );
  document.getElementById("feed-note").textContent = `${st.reason || ""}. Market hours: ${(st.market_hours || {}).reason || "—"}.`;
  const sigs = await api("/api/live/signals?limit=10");
  clear(document.getElementById("latest-signals")).append(signalTable(sigs.signals));
}

function signalTable(signals) {
  return table(["Cut-off", "Strategy", "Dir", "TF", "Status", "Bid / Ask", "Spread", "Stop", "Target", "Evidence / conflicts", "Outcome"],
    signals.map((s) => el("tr", {}, [
      td(fmtTime(s.cutoff, true)), td(`${s.strategy.name} v${s.strategy.version}`), td(s.direction), td(s.timeframe),
      td(s.status + (s.status_reason ? `: ${s.status_reason}` : "")), td(`${s.price.bid} / ${s.price.ask}`), td(s.price.spread),
      td(s.plan.stop), td(s.plan.target),
      el("td", {}, el("details", {}, [el("summary", { text: `${s.supporting.length} for, ${s.conflicting.length} against` }),
        el("ul", { class: "plain" }, [...s.supporting.map((c) => el("li", { text: `+ ${c.evidence}` })),
          ...s.conflicting.map((c) => el("li", { text: `- ${c.evidence}` })),
          ...s.invalidation.map((c) => el("li", { text: `invalidation: ${c.evidence}` }))])])),
      td(s.outcome ? s.outcome.verdict || s.outcome.status : "pending"),
    ])), "No strategy signal yet. Signals appear at step-candle closes while market data is LIVE.");
}

function renderChecks(v) {
  S.checksShown = true;
  clear(document.getElementById("verify-checks")).append(
    el("p", { class: "muted small", text: `Record ${v.id} (${v.started_at}), adapter ${v.adapter}${v.actual_terminal ? " = actual terminal" : " - NOT an actual-terminal run"}: ${v.overall}.` }),
    table(["Check", "Result", "Detail"], v.checks.map((c) => el("tr", {}, [td(c.label), td(c.status, `check-${c.status}`), td(c.detail)]))));
}

async function runVerification() {
  const btn = document.getElementById("run-verify");
  btn.disabled = true;
  say("Running the read-only terminal verification...");
  try {
    const v = await api("/api/live/verifications", { method: "POST", timeoutMs: 120000 });
    say(`Verification ${v.overall}${v.actual_terminal ? "" : " (not an actual-terminal run)"}.`, v.overall === "FAILED" ? "error" : "ok");
    renderChecks(v);
    await refreshOverview();
  } catch (err) { say(err.message, "error"); } finally { btn.disabled = false; }
}

// ------------------------------------------------------------------ monitor
async function loadStrategies() {
  const out = await api("/api/strategies");
  S.strategies = out.strategies;
  for (const id of ["mon-strategy", "st-strategy", "jf-strategy"]) {
    const sel = document.getElementById(id);
    const keep = id === "jf-strategy" ? [el("option", { value: "", text: "all" })] : [];
    clear(sel).append(...keep, ...S.strategies.map((s) => el("option", { value: s.id, text: `${s.name} v${s.version}` })));
  }
}

async function checkEligibility() {
  const sid = document.getElementById("mon-strategy").value;
  if (!sid) return;
  const e = await api(`/api/live/eligibility/${sid}`);
  const box = clear(document.getElementById("mon-eligibility"));
  box.append(el("p", { class: `notice ${e.eligible ? "ok" : "warn"}`, text: e.eligible ? `Validated by ${e.runs.length} Phase 3 run(s).` + (e.note ? ` ${e.note}` : "") : e.reason }));
  if (e.runs.length) box.append(table(["Run", "Kind", "Range", "Trades", "Net", "Expectancy R", "Sufficient"],
    e.runs.map((r) => el("tr", {}, [td(r.name || r.id), td(r.kind), td(`${(r.range.start || "").slice(0, 16)} → ${(r.range.end || "").slice(0, 16)}`),
      td(r.trades), pnl(r.net), td(r.expectancy_r), td(r.sufficient ? "yes" : "no")]))));
}

async function addMonitor() {
  const sid = Number(document.getElementById("mon-strategy").value);
  try {
    await api("/api/live/monitor", { method: "POST", body: { strategy_id: sid, mode: document.getElementById("mon-mode").value } });
    say("Monitoring starts at the next step-candle close.", "ok");
    await refreshMonitor();
  } catch (err) { say(err.message, "error"); }
}

async function patchMonitor(id, body) {
  try { await api(`/api/live/monitor/${id}`, { method: "PATCH", body }); await refreshMonitor(); } catch (err) { say(err.message, "error"); }
}

async function refreshMonitor() {
  if (!S.strategies.length) await loadStrategies();
  const out = await api("/api/live/monitor");
  const rows = out.monitored.map((m) => {
    const ev = m.evaluations || {};
    const acct = m.account || {};
    const actions = el("td", {}, m.status === "stopped" ? "stopped" : [
      el("button", { type: "button", class: "btn", text: m.status === "active" ? "Pause" : "Resume",
        onclick: () => patchMonitor(m.id, { status: m.status === "active" ? "paused" : "active" }) }),
      el("button", { type: "button", class: "btn", text: m.mode === "paper" ? "Observe only" : "Paper mode",
        onclick: () => patchMonitor(m.id, { mode: m.mode === "paper" ? "observe" : "paper" }) }),
      el("button", { type: "button", class: "btn", text: "Stop", onclick: () => patchMonitor(m.id, { status: "stopped" }) }),
    ]);
    return el("tr", {}, [td(`${m.strategy_name} v${m.version}`), td(m.mode), td(m.status), td(m.step_timeframe),
      td(fmtTime(m.started_at, true)), td(Object.entries(ev).map(([k, v]) => `${k} ${v}`).join(", ") || "none yet"),
      td((m.state || {}).last_reason || (m.state || {}).last_outcome), td(m.open_trades), td(acct.equity ? num(acct.equity) : "—"), actions]);
  });
  clear(document.getElementById("monitored")).append(
    table(["Version", "Mode", "Status", "Step", "Since", "Cut-offs evaluated", "Last result", "Open", "Validation equity", ""], rows,
      "No strategy version is monitored."),
    el("p", { class: "muted small", text: out.monitor ? `Monitor: ${out.monitor.running ? "running" : "not running"}, last cycle ${fmtTime(out.monitor.last_cycle, true)}${out.monitor.last_error ? `, last error: ${out.monitor.last_error}` : ""}.` : "" }));
}

// ------------------------------------------------------------------ positions
async function refreshPositions() {
  const [open, closed] = await Promise.all([api("/api/live/trades?status=open"), api("/api/live/trades?status=closed&limit=200")]);
  clear(document.getElementById("open-trades")).append(table(["Strategy", "Side", "Lots", "Entry", "Opened", "Stop", "Target", "Mark", "Unrealized", "MFE", "MAE"],
    open.trades.map((t) => el("tr", {}, [td(`${t.strategy_name} v${t.version}`), td(t.side), td(t.lots), td(t.open_price),
      td(fmtTime(t.open_time, true)), td(t.stop_loss), td(t.take_profit), td(t.mark_price), pnl(t.unrealized_pnl), td(t.mfe), td(t.mae)])),
    "No open SIMULATED position."));
  clear(document.getElementById("closed-trades")).append(table(["Strategy", "Side", "Lots", "Entry", "Exit", "Reason", "Held (min)", "Net", "MFE", "MAE", ""],
    closed.trades.map((t) => el("tr", {}, [td(`${t.strategy_name} v${t.version}`), td(t.side), td(t.lots), td(t.open_price), td(t.close_price),
      td(t.close_reason), td(t.holding_minutes), pnl(t.realized_net), td(t.mfe), td(t.mae),
      el("td", {}, el("button", { type: "button", class: "btn", text: "Evaluation", onclick: (e) => showEvaluation(t.id, e.target.closest("tr")) }))])),
    "No closed SIMULATED position."));
}

async function showEvaluation(tradeId, row) {
  const out = await api(`/api/live/trades/${tradeId}`);
  const ev = out.evaluation;
  const body = ev ? [el("p", { class: "small", text: ev.summary }),
    el("ol", { class: "plain" }, ev.answers.map((a) => el("li", {}, [el("strong", { text: `${a.question} ` }), `[${a.verdict}] ${a.answer}`,
      el("ul", { class: "plain" }, a.evidence.map((x) => el("li", { class: "muted", text: x })))]))),
    el("p", { class: "small", text: `Went well: ${ev.went_well.join("; ") || "—"} | Went wrong: ${ev.went_wrong.join("; ") || "—"}` })]
    : [el("p", { class: "muted small", text: "No evaluation stored yet (it is written when the trade closes)." })];
  const next = row.nextElementSibling;
  if (next && next.classList.contains("eval-row")) { next.remove(); return; }
  row.after(el("tr", { class: "eval-row" }, el("td", { colspan: "11" }, body)));
}

// ------------------------------------------------------------------ performance
async function refreshPerformance() {
  const [eq, perf] = await Promise.all([api("/api/live/equity"), api("/api/live/performance")]);
  const box = document.getElementById("equity-chart");
  if (!S.chart) S.chart = createMultiLineChart(box);
  S.chart.setSeries(eq.lines.map((l, i) => ({ name: l.name, color: COLORS[i % COLORS.length],
    points: l.points.map((p) => ({ time: Math.floor(Date.parse(p.time) / 1000), value: p.equity })) })));
  document.getElementById("equity-note").textContent = eq.lines.length ? eq.label : "No validation account yet (monitor a version in paper mode).";
  const rows = perf.strategies.map((s) => {
    const b = s.backtest || {};
    const lv = s.live;
    return el("tr", {}, [td(`${s.name} v${s.version}`), td(s.mode), td(b.kind ? `${b.kind} ${b.trades} trades` : "—"), pnl(b.net),
      td(b.expectancy_r), td(pct(b.win_rate)), td(lv.trades), pnl(lv.net), td(lv.expectancy_r), td(pct(lv.win_rate)),
      td(`${s.signals.total} (${s.signals.false} false / ${s.signals.measured} measured)`), td(lv.sufficient ? "yes" : lv.reason)]);
  });
  clear(document.getElementById("comparison")).append(
    table(["Version", "Mode", "Backtest", "BT net", "BT exp. R", "BT win", "Live trades", "Live net", "Live exp. R", "Live win", "Signals", "Live sample"],
      rows, "No monitored version yet."), el("p", { class: "muted small", text: perf.note }));
}

// ------------------------------------------------------------------ journal
function journalQuery() {
  const q = new URLSearchParams();
  const v = (id) => document.getElementById(id).value;
  if (v("jf-start")) q.set("start", `${v("jf-start")}T00:00:00Z`);
  if (v("jf-end")) q.set("end", `${v("jf-end")}T23:59:59Z`);
  for (const [id, key] of [["jf-strategy", "strategy_id"], ["jf-session", "session"], ["jf-tf", "timeframe"], ["jf-outcome", "outcome"]]) {
    if (v(id)) q.set(key, v(id));
  }
  return q.toString();
}

async function refreshJournal() {
  if (!S.strategies.length) await loadStrategies();
  const qs = journalQuery();
  document.getElementById("journal-csv").setAttribute("href", `/api/live/journal/export.csv${qs ? `?${qs}` : ""}`);
  const out = await api(`/api/live/journal${qs ? `?${qs}` : ""}`);
  clear(document.getElementById("journal")).append(
    table(["Time", "Strategy", "Session", "TF", "Dir", "Status", "Outcome", "Net", "MFE / MAE", "Invalidated", "Went well / wrong", ""],
      out.entries.map((e) => el("tr", {}, [td(fmtTime(e.timestamp, true)), td(`${e.strategy.name} v${e.strategy.version}`),
        td(e.session.join(" / ") || "none"), td(e.timeframe), td(e.direction), td(e.signal_status), td(e.outcome), pnl(e.net_pnl),
        td(e.mfe ? `${e.mfe} / ${e.mae}` : "—"), td(e.invalidated ? e.invalidated_how || "yes" : "no"),
        td([...e.went_well.map((x) => `+ ${x}`), ...e.went_wrong.map((x) => `- ${x}`)].join("; ")),
        el("td", {}, e.trade_id ? el("button", { type: "button", class: "btn", text: "Evaluation", onclick: (ev) => showEvaluation(e.trade_id, ev.target.closest("tr")) }) : "")])),
      "No journal entry matches."), el("p", { class: "muted small", text: out.label }));
}

// ------------------------------------------------------------------ improvement
async function runStudy() {
  if (!S.strategies.length) await loadStrategies();
  const sid = Number(document.getElementById("st-strategy").value);
  try {
    const out = await api("/api/research/studies", { method: "POST", body: {
      strategy_id: sid, discovery_pct: Number(document.getElementById("st-discovery").value),
      use_live_evidence: document.getElementById("st-live").checked,
      assumptions: { min_trades_for_ratios: Number(document.getElementById("st-min").value) } } });
    pollStudy(out.study.id);
  } catch (err) { say(err.message, "error"); }
}

function pollStudy(id) {
  clearInterval(S.studyPoll);
  S.studyPoll = setInterval(async () => {
    const s = await api(`/api/research/studies/${id}`);
    document.getElementById("st-progress").textContent = `${s.status} ${s.progress}/${s.total}`;
    if (!["queued", "running"].includes(s.status)) {
      clearInterval(S.studyPoll);
      S.studyPoll = null;
      renderStudy(s);
      await refreshProposals();
    }
  }, 1000);
}

function renderStudy(s) {
  const box = clear(document.getElementById("study"));
  if (s.status !== "complete") { box.append(el("p", { class: "notice error", text: s.error || s.status })); return; }
  const r = s.result;
  const w = r.windows;
  box.append(el("p", { class: "small", text: `Study ${s.id} of ${s.params.strategy.name} v${s.params.strategy.version} on ${s.params.series} (${fmtTime(s.finished_at, true)}).` }),
    el("p", { class: "small", text: `Discovery ${w.discovery.start.slice(0, 16)} → ${w.discovery.end.slice(0, 16)} | embargo | holdout ${w.holdout.start.slice(0, 16)} → ${w.holdout.end.slice(0, 16)}. ${w.live_evidence_used ? "Live-paper evidence used (after the holdout)." : w.live_note || ""}` }),
    el("p", { class: "small", text: `Discovery backtest: ${r.discovery.trades} trades, net ${num(r.discovery.net)}. Spread stress: x1 ${num(r.spread_stress["1"])}, x1.5 ${num(r.spread_stress["1.5"])}, x2 ${num(r.spread_stress["2"])}.` }),
    table(["Finding", "n", "Detail"], r.findings.map((f) => el("tr", {}, [td(f.title), td(f.n), td(f.detail)])), "No finding."),
    el("p", { class: "muted small", text: r.disclaimer }));
}

async function decide(pid, action, ack = false) {
  try {
    const out = await api(`/api/proposals/${pid}/${action}`, { method: "POST", body: { acknowledge_insufficient_sample: ack } });
    say(action === "approve" ? `Saved as version ${out.new_version}. Add it to the monitor to validate it live.` : "Proposal rejected.", "ok");
    await loadStrategies();
    await refreshProposals();
  } catch (err) { say(err.message, "error"); }
}

async function refreshProposals() {
  if (!S.strategies.length) await loadStrategies();
  const studies = await api("/api/research/studies");
  const latest = studies.studies[0];
  if (latest && !["queued", "running"].includes(latest.status) && !S.studyPoll) renderStudy(await api(`/api/research/studies/${latest.id}`));
  else if (latest && ["queued", "running"].includes(latest.status) && !S.studyPoll) pollStudy(latest.id);
  const out = await api("/api/proposals");
  const box = clear(document.getElementById("proposals"));
  if (!out.proposals.length) { box.append(el("p", { class: "muted small", text: "No proposals yet. Run a study." })); return; }
  box.append(el("p", { class: "muted small", text: out.note }));
  for (const p of out.proposals) {
    const v = p.validation || {};
    const b = v.base || {};
    const c = v.candidate || {};
    const ack = el("input", { type: "checkbox", id: `ack-${p.id}` });
    const open = ["validated", "inconclusive"].includes(p.status);
    box.append(el("details", {}, [
      el("summary", { text: `[${p.status}] ${p.strategy_name} v${p.base_version}: ${p.title}${p.resulting_version ? ` → v${p.resulting_version}` : ""}` }),
      el("p", { class: "small", text: p.explanation }), el("p", { class: "small num", text: `Change: ${p.diff}` }),
      table(["Holdout", "Trades", "Net", "Win rate", "Expectancy R", "Max DD"], [
        el("tr", {}, [td(`base v${p.base_version}`), td(b.trades), pnl(b.net), td(pct(b.win_rate)), td(b.expectancy_r), td(b.max_drawdown)]),
        el("tr", {}, [td("candidate"), td(c.trades), pnl(c.net), td(pct(c.win_rate)), td(c.expectancy_r), td(c.max_drawdown)])]),
      el("p", { class: "small", text: `Verdict: ${p.verdict} - ${v.why || ""}` }),
      el("ul", { class: "plain" }, (p.limitations || []).map((x) => el("li", { class: "muted", text: x }))),
      open ? el("div", { class: "toolbar" }, [
        p.status === "inconclusive" ? el("label", { class: "small" }, [ack, " I understand the holdout sample is too small"]) : null,
        el("button", { type: "button", class: "btn", text: "Approve (new version)", onclick: () => decide(p.id, "approve", ack.checked) }),
        el("button", { type: "button", class: "btn", text: "Reject", onclick: () => decide(p.id, "reject") })]) :
        el("p", { class: "muted small", text: p.decided_at ? `Decided ${fmtTime(p.decided_at, true)}${p.decision_note ? `: ${p.decision_note}` : ""}` : "Not adoptable: the candidate did not improve on the unseen holdout." }),
    ]));
  }
}

// ------------------------------------------------------------------ assistant
async function refreshAssistant() {
  const st = await api("/api/assistant/status");
  document.getElementById("as-mode").textContent = st.llm.enabled ? `Claude mode (${st.llm.model}): ${st.llm.reason}` : st.llm.reason;
  const qbox = clear(document.getElementById("as-questions"));
  for (const q of st.questions) qbox.append(el("button", { type: "button", class: "btn", text: q, onclick: () => ask(q) }));
}

async function ask(question) {
  const box = document.getElementById("as-answers");
  try {
    const out = await api("/api/assistant/ask", { method: "POST", body: { question }, timeoutMs: 120000 });
    box.prepend(el("div", { class: "qa" }, [el("p", { class: "small", text: `Q: ${out.question}` }),
      el("p", { class: "answer", text: out.answer }),
      el("p", { class: "muted small", text: `${out.mode} answer${out.note ? ` (${out.note})` : ""}; sources: ${(out.sources || []).slice(0, 12).join(", ") || "none"}${(out.sources || []).length > 12 ? " ..." : ""}` })]));
  } catch (err) { say(err.message, "error"); }
}

// ------------------------------------------------------------------ MT5 demo-account execution (optional)
async function demoLink(mid, enabled) {
  let confirm = null;
  if (enabled) {
    confirm = window.prompt("Orders for this strategy version will be placed on your MT5 DEMO account (virtual funds). " +
      "Type DEMO to confirm.");
    if (confirm === null) return;
  }
  try {
    const out = await api(`/api/live/demo/links/${mid}`, { method: "POST", body: { enabled, confirm } });
    say(enabled ? `Linked to the demo account on ${out.server}.${out.note ? " " + out.note : ""}` : "Unlinked.", out.note ? "warn" : "");
    await refreshDemo();
  } catch (err) { say(err.message, "error"); }
}

async function demoStop() {
  const close = document.getElementById("demo-stop-close").checked;
  if (!window.confirm(`Disable demo execution for every strategy${close ? " and close every open GoldScalp demo position" : ""}?`)) return;
  try {
    const out = await api("/api/live/demo/stop", { method: "POST", body: { close_positions: close } });
    say(`Demo execution stopped: ${out.links_disabled} link(s) disabled, ${out.closed.length} position(s) closed` +
      (out.failed.length ? `, ${out.failed.length} could not be closed` : "") + ".", out.failed.length ? "error" : "");
    await refreshDemo();
  } catch (err) { say(err.message, "error"); }
}

const money = (v) => (v === undefined || v === null ? "—" : num(v));

async function refreshDemo() {
  const st = await api("/api/live/demo");
  const a = st.account || {};
  clear(document.getElementById("demo-state")).append(el("span", { class: st.enabled_setting && a.demo ? "trend-up" : "trend-down",
    text: !st.enabled_setting ? "OFF (setting)" : a.demo ? "DEMO account ready" : "not ready" }));
  clear(document.getElementById("demo-grid")).append(
    stat("Setting", st.enabled_setting ? "enabled" : "disabled"),
    stat("Account type", a.trade_mode || "—", a.demo ? "trend-up" : a.trade_mode ? "trend-down" : ""),
    stat("Server", a.server || "—"),
    stat("Positions", a.margin_mode || "—"),
    stat("Algo Trading", a.algo_trading === undefined ? "—" : a.algo_trading ? "on" : "OFF", a.algo_trading ? "trend-up" : "trend-down"),
    stat("Demo balance", a.balance === undefined || a.balance === null ? "—" : `${num(a.balance)} ${a.currency || ""}`),
    stat("Demo equity", money(a.equity)),
    stat("Open demo positions", st.open),
  );
  const problems = [...(a.problems || [])];
  if (!st.enabled_setting) problems.unshift("Demo execution is switched off: set GOLDSCALP_DEMO_EXECUTION_ENABLED=true in .env and restart GoldScalp.");
  if (st.enabled_setting && a.available && a.algo_trading === false) problems.push("MT5's 'Algo Trading' button is off: the terminal will refuse orders until you switch it on.");
  if (st.last_error) problems.push(`Last error: ${st.last_error}`);
  clear(document.getElementById("demo-problems")).append(...problems.map((p) => el("li", { text: p })));
  const lim = st.limits;
  const linkRows = st.links.map((lk) => {
    const button = el("button", { type: "button", class: "btn", text: lk.enabled ? "Unlink" : "Link to demo",
      onclick: () => demoLink(lk.monitored_id, !lk.enabled) });
    if (!lk.enabled && (!st.enabled_setting || lk.mode !== "paper" || lk.status !== "active")) button.disabled = true;
    return el("tr", {}, [td(`${lk.strategy_name} v${lk.version}`), td(lk.mode), td(lk.status), td(lk.enabled ? "LINKED" : "off", lk.enabled ? "up" : ""),
      td(lk.enabled ? fmtTime(lk.enabled_at, true) : lk.disabled_at ? `off since ${fmtTime(lk.disabled_at, true)}` : ""),
      td(lk.enabled ? lk.server : lk.disabled_reason), el("td", {}, button)]);
  });
  clear(document.getElementById("demo-links")).append(
    table(["Version", "Mode", "Monitor", "Demo link", "Since", "Note", ""], linkRows,
      "No monitored strategy version. Add one on the Strategy Monitor tab (paper mode)."),
    el("p", { class: "muted small", text: `Limits: ${lim.max_lots} lots per order, ${lim.max_positions} open positions, ` +
      `entry within ${lim.max_entry_delay_seconds} s of the simulated entry, deviation ${lim.deviation_points} points, magic ${lim.magic}.` }));
  document.getElementById("demo-summary").textContent = `${st.closed_count} closed, net ${num(st.closed_net)} (demo funds)`;
  clear(document.getElementById("demo-trades")).append(table(
    ["Trade", "Status", "Side", "Lots", "Entry", "SL", "TP", "Opened", "Exit", "Closed", "Reason", "Net", "Message"],
    st.trades.map((d) => el("tr", {}, [td(`#${d.live_trade_id}`), td(d.status), td(d.side), td(d.volume), td(d.open_price), td(d.sl), td(d.tp),
      td(d.opened_at ? fmtTime(d.opened_at, true) : ""), td(d.close_price), td(d.closed_at ? fmtTime(d.closed_at, true) : ""), td(d.close_reason),
      pnl(d.profit === null ? null : Number(d.profit) + Number(d.commission || 0) + Number(d.swap || 0)), td(d.message)])),
    "No demo-account order yet."));
}

// ------------------------------------------------------------------ boot
async function boot() {
  try { const cfg = await api("/api/ui-config"); setDisplayZone(cfg.display_timezone); } catch (_) { /* default UTC */ }
  for (const b of document.querySelectorAll(".lab-tabs [role=tab]")) b.addEventListener("click", () => showTab(b.dataset.tab));
  document.getElementById("run-verify").addEventListener("click", runVerification);
  document.getElementById("demo-stop").addEventListener("click", demoStop);
  document.getElementById("mon-check").addEventListener("click", () => checkEligibility().catch((e) => say(e.message, "error")));
  document.getElementById("mon-add").addEventListener("click", addMonitor);
  document.getElementById("journal-filters").addEventListener("change", () => refreshJournal().catch((e) => say(e.message, "error")));
  document.getElementById("st-run").addEventListener("click", runStudy);
  document.getElementById("as-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = document.getElementById("as-input");
    if (input.value.trim().length >= 2) ask(input.value.trim());
  });
  await loadStrategies().catch((e) => say(e.message, "error"));
  showTab(storageGet("goldscalp:live-tab:v1", "overview"));
  S.timer = setInterval(() => { if (["overview", "positions", "monitor", "demo"].includes(S.tab) && !document.hidden) refresh(); }, 5000);
}

boot();
