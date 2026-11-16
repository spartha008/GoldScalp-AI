// Research candidates: published rule sets registered for further research (GET /api/monthly/research-candidates).
// Read-only: nothing here can start a backtest, a monitor or an order. The Strategy Lab form of a candidate is
// monitored and linked on the Live Validation page of the main app; this page only links to it.
import { api } from "./api.js";
import { compare, lineChart } from "./library.js";
import { clear, el } from "./ui.js";

const C = { data: null, charts: [], timer: null, main: null };
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const sign = (v, d) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}`;
const r3 = (v) => (v === null || v === undefined ? "—" : sign(v, 3));
const r2 = (v) => (v === null || v === undefined ? "—" : sign(v, 2));
const usd = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}$${Math.abs(v).toFixed(2)}`);
const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const day = (d) => `${Number(d.slice(8, 10))} ${MON[Number(d.slice(5, 7)) - 1]}`;
const chip = (text, t) => el("span", { class: `chip ${t}`, text });
const td = (text, cls = "") => el("td", { class: cls || null, text });
const n = (text, cls = "") => el("td", { class: `n ${cls}`.trim(), text });
const list = (items) => el("ul", { class: "plain" }, items.map((x) => el("li", { text: x })));

function table(head, rows, numericFrom = 1) {
  return el("div", { class: "table-wrap" }, el("table", { class: "compact data" }, [
    el("thead", {}, el("tr", {}, head.map((h, i) => el("th", { scope: "col", class: i >= numericFrom ? "n" : null, text: h })))),
    el("tbody", {}, rows)]));
}

export async function openCandidates() {
  const root = document.getElementById("cand-root");
  if (!C.data) {
    clear(root).append(el("p", { class: "muted small", text: "Loading the research candidates…" }));
    try {
      C.data = await api("/api/monthly/research-candidates", { timeoutMs: 60000 });
    } catch (err) {
      clear(root).append(el("p", { class: "notice warn", text: err.status === 404
        ? "Research candidates need the research service to be restarted once to switch them on. Restart it while no study is running."
        : `The research candidates could not be loaded: ${err.message}` }));
      return;
    }
    window.addEventListener("resize", () => { clearTimeout(C.timer); C.timer = setTimeout(drawCharts, 150); });
  }
  if (!C.main) {
    try {
      C.main = (await api("/api/ui-config")).main_app;
    } catch {
      C.main = null;                                            // no link to the main app is shown
    }
  }
  render(root);
}

function render(root) {
  C.charts = [];
  const cs = C.data.candidates;
  const active = cs.filter((c) => c.status !== "archived");
  const archived = cs.filter((c) => c.status === "archived");
  clear(root).append(el("p", { class: "notice warn compact", text: C.data.banner }),
    ...(active.length ? active.map(card) : [el("p", { class: "muted", text: "No active research candidate." })]),
    archived.length ? el("details", { class: "more cand-archived" }, [
      el("summary", { text: `Archived candidates (${archived.length}): kept with their rules and evidence, no longer researched` }),
      ...archived.map(card)]) : null);
  drawCharts();
}

function windowLabel(name, w) {
  const r = w.range || {};
  const span = r.start ? `${day(r.start)} – ${day(new Date(new Date(r.end) - 864e5).toISOString())} ${r.end.slice(0, 4)}` : "";
  return [name === "screen" ? "10-day screen" : "11-week context run", span];
}

function evidenceTable(ev) {
  const rows = Object.entries(ev.windows).map(([name, w]) => {
    const s = w.stats;
    const [label, span] = windowLabel(name, w);
    return el("tr", {}, [el("th", { scope: "row", class: "left" }, [label, el("div", { class: "small muted", text: span })]),
      n(s.trades), n(pct(s.win_rate)), n(r3(s.expectancy_r), tone(s.expectancy_r)), n(s.profit_factor === null ? "—" : s.profit_factor.toFixed(2)),
      n(usd(s.net_usd), tone(s.net_usd)), n(s.max_dd_r === null ? "—" : s.max_dd_r.toFixed(1)),
      n(r3(s.expectancy_r_costs_x2), tone(s.expectancy_r_costs_x2)), n(`${w.positive_days ?? 0} of ${w.days_traded}`)]);
  });
  return table(["Window", "Trades", "Win", "R/trade", "PF", "Net USD", "Max DD", "R, costs ×2", "Up days"], rows);
}

function legsTable(ev) {
  const rows = [];
  for (const [name, w] of Object.entries(ev.windows)) {
    for (const [leg, s] of Object.entries(w.legs || {})) {
      rows.push(el("tr", {}, [td(leg), td(name === "screen" ? "10-day screen" : "11 weeks"), n(s.trades), n(r3(s.expectancy_r), tone(s.expectancy_r)),
        n(usd(s.net_usd), tone(s.net_usd))]));
    }
  }
  return rows.length ? [el("h3", { text: "Its two legs" }), table(["Leg", "Window", "Trades", "R per trade", "Net USD"], rows, 2)] : [];
}

function monthsTable(ev) {
  const w = ev.windows.context;
  if (!w) return [];
  const rows = w.by_month.map((m) => el("tr", {}, [td(`${MON[Number(m.month.slice(5)) - 1]} ${m.month.slice(0, 4)}`), n(m.trades), n(pct(m.win_rate)),
    n(r2(m.sum_r), tone(m.sum_r)), n(usd(m.net_usd), tone(m.net_usd))]));
  return [el("h3", { text: "Month by month (11-week run)" }), table(["Month", "Trades", "Win rate", "Sum R", "Net USD"], rows)];
}

function historyBlock(ev) {
  const h = ev.history;
  if (!h) return [];
  const s = h.stats;
  const pf = (v) => (v === null || v === undefined ? "—" : v.toFixed(2));
  const span = h.spans.map((x) => `${day(x.start)} – ${day(new Date(new Date(x.end) - 864e5).toISOString())}`).join(" and ");
  const half = (from, to) => h.by_month.filter((m) => m.month.slice(5) >= from && m.month.slice(5) <= to)
    .reduce((a, m) => ({ trades: a.trades + m.trades, sum_r: a.sum_r + m.sum_r, net_usd: a.net_usd + m.net_usd }), { trades: 0, sum_r: 0, net_usd: 0 });
  const pooled = (label, x) => el("tr", {}, [td(label), n(x.trades), n(pct(x.win_rate)), n(r3(x.expectancy_r), tone(x.expectancy_r)), n(pf(x.profit_factor)),
    n(usd(x.net_usd), tone(x.net_usd)), n(r3(x.expectancy_r_costs_x2), tone(x.expectancy_r_costs_x2))]);
  const months = h.by_month.map((m) => el("tr", {}, [td(`${MON[Number(m.month.slice(5)) - 1]} ${m.month.slice(0, 4)}`), n(m.trades), n(pct(m.win_rate)),
    n(r2(m.sum_r), tone(m.sum_r)), n(usd(m.net_usd), tone(m.net_usd))]));
  const [h1, h2] = [half("01", "06"), half("07", "12")];
  return [el("h3", { text: `All of ${h.spans[0].start.slice(0, 4)} before the forward boundary` }),
    el("p", { class: "small", text: `Run after registration over ${span}: the history research had already used. It contains both windows above. Exploratory: it selects nothing and is not validation.` }),
    table(["Period", "Trades", "Win", "R/trade", "PF", "Net USD", "R, costs ×2"], [pooled("Whole run", s), ...Object.entries(h.legs || {}).map(([leg, x]) => pooled(leg, x))]),
    el("p", { class: "small" }, [el("strong", { text: "The result is uneven. " }),
      `January to June: ${r2(h1.sum_r)} R and ${usd(h1.net_usd)} over ${h1.trades} trades. July onward: ${r2(h2.sum_r)} R and ${usd(h2.net_usd)} over ${h2.trades} trades. ${h.positive_months} of ${h.months} months and ${h.positive_days} of ${h.days_traded} days made money.`]),
    table(["Month", "Trades", "Win rate", "Sum R", "Net USD"], months)];
}

function placebo(ev) {
  const p = ev.placebo;
  if (!p) return [];
  const row = (r) => el("tr", {}, [n(r.rank), td(`${r.enter_new_york} New York`), n(r.trades), n(usd(r.net_usd_per_trade), tone(r.net_usd_per_trade)),
    n(r.t_stat === null ? "—" : r.t_stat.toFixed(2)), n(pct(r.win_rate))]);
  const own = new Set(p.own_windows.map((r) => r.enter_new_york));
  const rows = [...p.top, ...p.bottom].map((r) => { const tr = row(r); if (own.has(r.enter_new_york)) tr.classList.add("cand-own"); return tr; });
  return [el("h3", { text: "Placebo check" }),
    el("p", { class: "small", text: `The same 30-minute short was started at every half hour of the New York day over the 11 weeks. Only ${p.positive_windows} of ${p.windows_tested} half hours made money; this candidate's two windows are highlighted. Run after the screen: exploratory, and it selects nothing.` }),
    table(["Rank", "Start", "Trades", "Net USD per trade", "t-statistic", "Win rate"], rows, 2)];
}

function labBlock(c) {
  if (!c.lab || !c.lab.length) return [];
  const items = c.lab.flatMap((s) => s.versions.map((v) => el("li", {}, [el("strong", { text: `${s.strategy_name}, ${v.label}.` }),
    ` ${v.difference || (v.as_screened ? "Places the same trades as the screened rule set." : "")}`])));
  const link = el("a", { text: "Live Validation" });
  if (C.main) link.href = new URL("/live", C.main).href;         // the main app's address, from this service's own ui-config
  const live = C.main ? [el("p", { class: "small" }, ["Whether a version is monitored or linked to the MT5 demo account is shown in the main app: ",
    link, " (tab Demo Execution, Linked strategy versions)."])] : [];
  return [el("h3", { text: "Strategy Lab form" }), el("ul", { class: "plain" }, items), ...live,
    el("p", { class: "small muted", text: "Added on 16 November 2026 at the owner's request, so it can be backtested in Strategy Lab. What it does in Live Validation or on the demo account is not a validation of the candidate." })];
}

function card(c) {
  const ev = c.evidence || {};
  const ok = c.check && c.check.fingerprint_ok && c.check.matches_declaration;
  const cls = ev.screen_classification;
  const chartBox = el("div", { class: "cand-chart" });
  if (ev.available && ev.windows.context) C.charts.push({ box: chartBox, days: ev.windows.context.by_day, label: c.id, screen: (ev.windows.screen || {}).range });
  const rules = el("dl", { class: "kv cand-kv" }, [
    el("dt", { text: "Source" }), el("dd", { text: c.source }),
    el("dt", { text: "Original rules" }), el("dd", { text: c.original_rules }),
    el("dt", { text: "As tested" }), el("dd", { text: c.implementation }),
    el("dt", { text: "Deviations" }), el("dd", {}, list(c.deviations)),
    el("dt", { text: "Execution" }), el("dd", { text: `${c.execution.bars}, ${c.execution.lots} lots; ${c.execution.costs}` }),
    el("dt", { text: "Stop and R" }), el("dd", { text: c.execution.r_unit })]);
  const evidence = ev.available ? [
    el("p", { class: "small muted", text: "Both windows are development data that research had already used. The 11-week run contains the 10 screen days. Neither is validation." }),
    evidenceTable(ev),
    cls ? el("p", { class: "small" }, ["Screen class: ", chip(cls.classification, "info"), ` ${cls.why.join("; ")}`]) : null,
    ...legsTable(ev), ...monthsTable(ev),
    el("h3", { text: "Cumulative R, day by day (11-week run)" }),
    el("p", { class: "small muted", text: "Sum of R after costs. R is a nominal unit here, because the source has no stop." }), chartBox,
    ...placebo(ev), ...historyBlock(ev)] : [el("p", { class: "muted small", text: "The stored screen results were not found, so only the registration record is shown." })];
  return el("section", { class: "panel cand", "aria-labelledby": `cand-${c.id}` }, [
    el("header", {}, [el("h2", { id: `cand-${c.id}`, text: c.name }), el("span", { class: "spacer" }), chip(c.id, "muted plain"),
      chip(c.status, c.status === "archived" ? "muted" : "info"), chip(ok ? "rules unchanged" : "rules changed", ok ? "ok" : "err")]),
    el("div", { class: "body" }, [
      c.archived ? el("p", { class: "notice compact" }, [el("strong", { text: `Archived ${day(c.archived.on)} ${c.archived.on.slice(0, 4)}. ` }),
        c.archived.reason, c.archived.strategy_lab ? ` ${c.archived.strategy_lab}` : ""]) : null,
      el("p", { class: "cand-lede", text: c.assessment }),
      el("div", { class: "cand-cols" }, [el("div", { class: "cand-col" }, [el("h3", { text: "Rules, exactly as screened" }), rules, ...labBlock(c)]),
        el("div", { class: "cand-col" }, [el("h3", { text: "Evidence at registration" }), ...evidence])]),
      el("details", { class: "more", open: true }, [el("summary", { text: "Caveats and open questions" }),
        el("div", { class: "cand-two" }, [el("div", {}, [el("h3", { text: "Caveats" }), list(c.caveats)]),
          el("div", {}, [el("h3", { text: "Questions for a proper test" }), list(c.next_questions)])])]),
      el("details", { class: "more" }, [el("summary", { text: "What a research candidate may and may not do" }), list(c.restrictions)]),
      el("div", { class: "toolbar" }, [
        el("button", { type: "button", class: "btn", text: "Compare in the Research library",
          onclick: () => { compare([`p7s2-context:${c.id}`, `p7s2-screen:${c.id}`, "p7s2-context:BASE-DRIFT"]); location.hash = "#library"; } }),
        el("span", { class: "small muted", text: `Registered ${c.registered} · ${c.origin.stage} · fingerprint ${c.fingerprint.slice(0, 12)} · config/research/candidates/${c.id}.json` })])])]);
}

function drawCharts() {
  for (const ch of C.charts) {
    const width = Math.max(320, ch.box.clientWidth || 640);
    const xs = ch.days.map((d) => d.day);
    const pts = new Map(ch.days.map((d) => [d.day, { y: d.cum_r, tip: `${r2(d.cum_r)} R (day ${r2(d.sum_r)} R, ${d.trades} trades, ${usd(d.net_usd)})` }]));
    clear(ch.box).append(lineChart([{ slot: 1, label: ch.label, pts }], xs, width, { yFmt: (v) => r2(v), tickFmt: day }));
  }
}
