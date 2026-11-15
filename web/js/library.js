// Research library: every Phase 5 and Phase 6 study, normalised by the research service (GET /api/monthly/library),
// compared side by side with charts, plus the written research reports. Read-only: nothing here starts, changes or
// re-scores a study. Classes and verdicts are shown exactly as the studies recorded them.
import { api } from "./api.js";
import { clear, el, safeHref, storageGet, storageSet } from "./ui.js";

const MAX = 8;                                       // categorical slots s1..s8 (validated dark palette, see tokens.css)
const KEY = "goldscalp.library.selection";
const SVG = "http://www.w3.org/2000/svg";
const L = { data: null, byId: new Map(), sel: [], query: "", phase: "all", align: "calendar", docs: [], resize: null };
const PHASES = [["all", "All"], ["5", "5 · Monthly"], ["5.1", "5.1 · Risk"], ["5.2", "5.2 · Robustness"], ["5.3", "5.3 · Validation"], ["6", "6 · Discovery"]];
const PHASE_TITLE = [["5", "Phase 5 · Fixed-lot monthly evaluation"], ["5.1", "Phase 5.1 · Hypothetical risk scenarios"],
  ["5.2", "Phase 5.2 · Robustness research"], ["5.3", "Phase 5.3 · Frozen validation"], ["6", "Phase 6 · New strategy discovery"]];
const CLASS_TONE = { promising: "ok", inconclusive: "warn", "inconclusive (not promising)": "warn", rejected: "err" };
const STATUS_TONE = { COMPLETED: "ok", RUNNING: "run", READY: "muted", INCOMPLETE: "warn", FAILED: "err" };

const r3 = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(3)}`);
const r2 = (v) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}`);
const f2 = (v) => (v === null || v === undefined ? "—" : Number(v).toFixed(2));
const pct = (v) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`);
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const chip = (text, t) => el("span", { class: `chip ${t || "info"}`, text });
const classChip = (c) => (c ? chip(c, CLASS_TONE[c] || "info") : null);
const swatch = (slot) => el("span", { class: `lib-swatch s${slot}`, "aria-hidden": "true" });
const numTd = (text, cls = "") => el("td", { class: `n ${cls}`.trim(), text });

function s(tag, attrs = {}, children = []) {                       // SVG element (presentation attributes only)
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  for (const c of [].concat(children)) if (c) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

// ------------------------------------------------------------------ data and selection
function slotOf(id) { const x = L.sel.find((e) => e.id === id); return x ? x.slot : null; }
function selected() { return L.sel.map((e) => ({ ...L.byId.get(e.id), slot: e.slot })).filter((r) => r.id); }
function save() { storageSet(KEY, JSON.stringify(L.sel)); }

function add(id) {
  if (slotOf(id) || L.sel.length >= MAX || !L.byId.has(id)) return false;
  const used = new Set(L.sel.map((e) => e.slot));
  const slot = [1, 2, 3, 4, 5, 6, 7, 8].find((n) => !used.has(n));      // colour follows the result, never its rank
  L.sel.push({ id, slot });
  return true;
}
function remove(id) { L.sel = L.sel.filter((e) => e.id !== id); }
function setSelection(ids) { L.sel = []; for (const id of ids) add(id); save(); renderAll(); }
function toggle(id, on) { if (on) add(id); else remove(id); save(); renderAll(); }

function studyOf(r) { return L.data.studies.find((x) => x.id === r.study); }
function phaseLabel(r) { const st = studyOf(r); return st ? `${st.phase === "6" ? "Phase 6" : `Phase ${st.phase}`} · ${st.title}` : r.study; }

async function load() {
  const root = document.getElementById("lib-root");
  try {
    L.data = await api("/api/monthly/library", { timeoutMs: 60000 });
  } catch (err) {
    clear(root).append(el("p", { class: "notice warn", text: err.status === 404
      ? "The research library needs the research service to be restarted once to switch it on. Restart it while no study is running; nothing is lost."
      : `The library could not be loaded: ${err.message}` }));
    return false;
  }
  L.byId = new Map(L.data.studies.flatMap((st) => st.results.map((r) => [r.id, r])));
  L.docs = L.data.documents || [];
  try { const saved = JSON.parse(storageGet(KEY, "[]")); L.sel = []; for (const e of saved) if (L.byId.has(e.id) && !slotOf(e.id)) L.sel.push(e); }
  catch (_) { L.sel = []; }
  return true;
}

// ------------------------------------------------------------------ layout
export async function openLibrary() {
  const root = document.getElementById("lib-root");
  if (!L.data) {
    clear(root).append(el("p", { class: "muted small", text: "Loading every study…" }));
    if (!(await load())) return;
    clear(root).append(
      el("div", { class: "lib-layout" }, [
        el("section", { class: "panel lib-browse", "aria-labelledby": "lib-st-title" }, [
          el("header", {}, [el("h2", { id: "lib-st-title", text: "Studies" }), el("span", { class: "spacer" }),
            el("span", { class: "badge", id: "lib-count" }),
            el("button", { type: "button", class: "btn ghost sm", id: "lib-refresh", text: "Refresh", onclick: refresh })]),
          el("div", { class: "body" }, [
            el("label", { class: "lib-search" }, [el("span", { class: "sr-only", text: "Search studies and results" }),
              el("input", { type: "search", id: "lib-q", placeholder: "Search: VOL-C, R2, rejected, sealed…", autocomplete: "off",
                oninput: (e) => { L.query = e.target.value.trim().toLowerCase(); renderBrowse(); } })]),
            el("div", { class: "lib-phases", role: "group", "aria-label": "Filter by phase" }, PHASES.map(([k, t]) =>
              el("button", { type: "button", class: "btn sm", "data-phase": k, "aria-pressed": String(k === L.phase), text: t,
                onclick: () => { L.phase = k; renderBrowse(); } }))),
            el("div", { id: "lib-studies", class: "lib-studies" })])]),
        el("section", { class: "panel lib-compare", "aria-labelledby": "lib-cmp-title" }, [
          el("header", {}, [el("h2", { id: "lib-cmp-title", text: "Comparison" }), el("span", { class: "spacer" }),
            el("span", { class: "badge", id: "lib-selcount" }),
            el("button", { type: "button", class: "btn ghost sm", id: "lib-clear", text: "Clear", onclick: () => setSelection([]) })]),
          el("div", { class: "body", id: "lib-cmp" })])]),
      el("section", { class: "panel", "aria-labelledby": "lib-docs-title" }, [
        el("header", {}, [el("h2", { id: "lib-docs-title", text: "Research reports" }), el("span", { class: "spacer" }),
          el("span", { class: "badge kind-historical", text: "Read-only" })]),
        el("div", { class: "body" }, [el("div", { id: "lib-docs" }), el("div", { id: "lib-reader", hidden: true })])]));
    if (!L.resize && "ResizeObserver" in window) {
      let t = null;
      let lastWidth = 0;                               // re-draw on width changes only (height changes come from the charts)
      L.resize = new ResizeObserver(([e]) => {
        const w = Math.round(e.contentRect.width);
        if (w === lastWidth) return;
        lastWidth = w; clearTimeout(t); t = setTimeout(renderCharts, 120);
      });
      L.resize.observe(document.getElementById("lib-cmp"));
    }
  }
  renderAll();
  renderDocs();
}

async function refresh() {
  L.data = null;
  await load();
  renderAll();
  renderDocs();
}

function renderAll() { if (!L.data) return; renderBrowse(); renderCompare(); }

// ------------------------------------------------------------------ study browser
function matches(st, r) {
  if (!L.query) return true;
  const hay = [st.title, st.id, st.period, r && r.label, r && r.strategy, r && r.class, r && r.role, ...(st.flags || []), st.kind]
    .filter(Boolean).join(" ").toLowerCase();
  return L.query.split(/\s+/).every((w) => hay.includes(w));
}

function resultRow(r) {
  const on = Boolean(slotOf(r.id));
  const full = !on && L.sel.length >= MAX;
  const box = el("input", { type: "checkbox", checked: on, disabled: full || r.status !== "COMPLETED", "aria-label": `Compare ${r.label}`,
    onchange: (e) => toggle(r.id, e.target.checked) });
  const e = r.pooled.expectancy_r;
  return el("li", { class: `lib-result${on ? " on" : ""}` }, [
    el("label", {}, [box, on ? swatch(slotOf(r.id)) : null, el("span", { class: "lbl", text: r.label })]),
    r.status !== "COMPLETED" ? chip(r.status, STATUS_TONE[r.status]) : classChip(r.class),
    el("span", { class: `num small ${tone(e)}`, title: "R per trade after costs", text: r.status === "COMPLETED" ? r3(e) : "—" }),
    el("span", { class: "num small muted", text: r.pooled.trades === null ? "" : `${r.pooled.trades} tr` }),
    el("button", { type: "button", class: "btn ghost sm lib-across", title: `Compare ${r.strategy} ${r.timeframe || ""} across every study`,
      "aria-label": `Compare ${r.strategy} ${r.timeframe || ""} across every study`, text: "across",
      onclick: () => setSelection(across(r)) })]);
}

function across(r) {
  const all = [...L.byId.values()].filter((x) => x.strategy === r.strategy && x.timeframe === r.timeframe && x.status === "COMPLETED");
  const ordered = [r, ...all.filter((x) => x.id !== r.id)];
  return ordered.slice(0, MAX).map((x) => x.id);
}

function renderBrowse() {
  const box = document.getElementById("lib-studies");
  if (!box) return;
  for (const b of document.querySelectorAll(".lib-phases button")) b.setAttribute("aria-pressed", String(b.dataset.phase === L.phase));
  document.getElementById("lib-count").textContent = `${L.data.studies.length} studies · ${L.data.results} results`;
  clear(box);
  let shown = 0;
  for (const [phase, title] of PHASE_TITLE) {                    // array, not object: keys like "5" would sort first
    if (L.phase !== "all" && L.phase !== phase) continue;
    const studies = L.data.studies.filter((st) => st.phase === phase)
      .sort((a, b) => (b.results.length > 0) - (a.results.length > 0))  // comparable studies first; the rest stay listed
      .map((st) => ({ st, rs: st.results.filter((r) => matches(st, r)) }))
      .filter(({ st, rs }) => rs.length || matches(st, null));
    if (!studies.length) continue;
    box.append(el("h3", { class: "lib-phase-title", text: title }));
    for (const { st, rs } of studies) {
      shown += 1;
      const open = Boolean(L.query) || rs.some((r) => slotOf(r.id)) || (st.results.length > 0 && st.results.length <= 6);
      const head = el("summary", {}, [
        el("span", { class: "t", text: st.title }),
        el("span", { class: "meta" }, [st.status !== "COMPLETED" ? chip(st.status, STATUS_TONE[st.status]) : null,
          ...(st.flags || []).map((f) => chip(f, f.startsWith("HYPOTHETICAL") ? "warn" : "muted")),
          el("span", { class: "small muted", text: [st.period, st.results.length ? `${st.results.length} results` : "no comparable results"].filter(Boolean).join(" · ") })])]);
      const body = [st.summary ? el("p", { class: "small muted", text: st.summary }) : null,
        st.error ? el("p", { class: "small down", text: `Error: ${st.error}` }) : null,
        rs.length ? el("ul", { class: "lib-results" }, rs.map(resultRow)) : null,
        el("p", { class: "small muted lib-src", text: `${st.id}${st.source ? ` · ${st.source}` : ""}` })];
      box.append(el("details", { class: "lib-study", open }, [head, ...body]));
    }
  }
  if (!shown) box.append(el("p", { class: "muted small", text: "Nothing matches the search." }));
}

// ------------------------------------------------------------------ comparison
function presets() {
  const all = [...L.byId.values()].filter((r) => r.status === "COMPLETED");
  const pick = (f) => all.filter(f).slice(0, MAX).map((r) => r.id);
  const sealed = all.filter((r) => r.study.startsWith("sr_") && !r.label.includes("(dev)"));
  const out = [];
  if (sealed.length) out.push(["H6.4 sealed vs its control", sealed.map((r) => r.id).slice(0, MAX)]);
  out.push(["VOL-C everywhere (M5)", pick((r) => r.strategy === "VOL-C" && r.timeframe === "M5")]);
  out.push(["Phase 6 development baselines", pick((r) => r.phase === "6" && r.study.startsWith("dt_"))]);
  out.push(["Phase 5.3 candidates vs originals (M5)", pick((r) => r.phase === "5.3" && r.timeframe === "M5")]);
  out.push(["Phase 5.2 redesigns rated promising or inconclusive", pick((r) => r.phase === "5.2" && r.role.startsWith("redesigned") && r.class !== "rejected")]);
  return out.filter(([, ids]) => ids.length);
}

function renderCompare() {
  const box = document.getElementById("lib-cmp");
  if (!box) return;
  const rs = selected();
  document.getElementById("lib-selcount").textContent = `${rs.length} of ${MAX}`;
  document.getElementById("lib-clear").disabled = !rs.length;
  clear(box);
  if (!rs.length) {
    box.append(el("p", { class: "muted", text: "Pick up to eight results from the studies list (tick a box), use “across” to follow one strategy through every study, or start from a ready-made comparison:" }),
      el("div", { class: "toolbar" }, presets().map(([t, ids]) => el("button", { type: "button", class: "btn", text: t, onclick: () => setSelection(ids) }))));
    return;
  }
  box.append(
    el("ul", { class: "lib-legend", "aria-label": "Selected results" }, rs.map((r) => el("li", {}, [swatch(r.slot),
      el("span", { class: "lbl", text: r.label }), el("span", { class: "small muted", text: `Phase ${r.phase}` }),
      el("button", { type: "button", class: "btn ghost sm", "aria-label": `Remove ${r.label}`, text: "×", onclick: () => toggle(r.id, false) })]))),
    ...[caveats(rs), metricsTable(rs), el("div", { id: "lib-charts", class: "lib-charts" }), provenance(rs)].filter(Boolean));
  renderCharts();
}

function caveats(rs) {
  const phases = new Set(rs.map((r) => r.phase));
  const flags = new Set(rs.flatMap((r) => r.flags));
  const notes = [];
  if (phases.size > 1 || new Set(rs.map((r) => r.study)).size > 1)
    notes.push("These results come from different months and studies. Compare shapes and signs with care: a sealed or validation result is independent evidence, a development result is not.");
  if (flags.has("development data")) notes.push("Development-data results helped design later tests; they are not independent validation.");
  if (flags.has("HYPOTHETICAL risk scenario")) notes.push("Risk-scenario results use hypothetical compounded percentage sizing, never a real-account setting.");
  return notes.length ? el("div", { class: "notice compact" }, notes.map((n) => el("p", { text: n }))) : null;
}

function monthStats(r) {
  const ms = Object.values(r.months).filter((m) => m.status === "COMPLETED" && m.sum_r !== null && m.trades);
  return { pos: ms.filter((m) => m.sum_r > 0).length, n: ms.length };
}

function metricsTable(rs) {
  const head = ["Result", "Study", "TF", "Class", "Trades", "Win rate", "R/trade", "×2 costs", "×3 costs", "PF", "Sum R", "Max DD (R)", "Months +"];
  const rows = rs.map((r) => {
    const p = r.pooled;
    const m = monthStats(r);
    return el("tr", {}, [
      el("th", { scope: "row", class: "left" }, el("span", { class: "lib-rowlabel" }, [swatch(r.slot), el("span", { text: r.label })])),
      el("td", { class: "truncate", title: phaseLabel(r), text: phaseLabel(r) }),
      el("td", { text: r.timeframe || "—" }),
      el("td", { class: "left" }, classChip(r.class) || el("span", { class: "muted small", text: r.role && r.role.includes("comparison") ? "comparison" : "—" })),
      numTd(p.trades ?? "—"), numTd(pct(p.win_rate)), numTd(r3(p.expectancy_r), tone(p.expectancy_r)),
      numTd(r3(p.expectancy_r_costs_x2), tone(p.expectancy_r_costs_x2)), numTd(r3(p.expectancy_r_costs_x3), tone(p.expectancy_r_costs_x3)),
      numTd(f2(p.profit_factor)), numTd(r2(p.sum_r), tone(p.sum_r)), numTd(f2(p.max_dd_r)), numTd(m.n ? `${m.pos} / ${m.n}` : "—")]);
  });
  return el("div", { class: "table-wrap" }, el("table", { class: "compact data lib-metrics" }, [
    el("caption", { class: "sr-only", text: "Pooled metrics of the selected results (R after costs)" }),
    el("thead", {}, el("tr", {}, head.map((h, i) => el("th", { scope: "col", class: i >= 4 ? "n" : null, text: h })))),
    el("tbody", {}, rows)]));
}

function provenance(rs) {
  return el("details", { class: "more" }, [el("summary", { text: "Where each result comes from" }),
    el("ul", { class: "plain small lib-prov" }, rs.map((r) => el("li", {}, [swatch(r.slot), el("strong", { text: r.label }),
      ` — ${phaseLabel(r)}. ${r.scope || ""}${r.role ? ` · ${r.role}` : ""}${r.flags.length ? ` · ${r.flags.join(", ")}` : ""}`,
      r.why ? el("span", { class: "muted", text: ` · ${r.class ? `${r.class}: ` : ""}${r.why}` }) : null])))]);
}

// ------------------------------------------------------------------ charts
function monthsBetween(a, b) {
  const out = [];
  let [y, m] = a.split("-").map(Number);
  const [by, bm] = b.split("-").map(Number);
  while (y < by || (y === by && m <= bm)) { out.push(`${y}-${String(m).padStart(2, "0")}`); m += 1; if (m > 12) { m = 1; y += 1; } }
  return out;
}

function axisMonths(rs) {
  if (L.align === "aligned") {
    const n = Math.max(0, ...rs.map((r) => Object.keys(r.months).length));
    return Array.from({ length: n }, (_, i) => `M${i + 1}`);
  }
  const keys = rs.flatMap((r) => Object.keys(r.months)).map((k) => k.slice(0, 7)).sort();
  return keys.length ? monthsBetween(keys[0], keys[keys.length - 1]) : [];
}

function monthKeyFor(r, x) {               // the result's month at axis position x (calendar month or Mn)
  const ks = Object.keys(r.months).sort();
  if (L.align === "aligned") return ks[Number(x.slice(1)) - 1];
  return ks.find((k) => k.slice(0, 7) === x);
}

function renderCharts() {
  const box = document.getElementById("lib-charts");
  if (!box) return;
  const rs = selected();
  clear(box);
  if (!rs.length) return;
  const width = Math.max(320, box.clientWidth || 640);
  const xs = axisMonths(rs);
  const alignToggle = el("div", { class: "seg lib-seg", role: "group", "aria-label": "Month axis" }, [["calendar", "Calendar months"], ["aligned", "Months since start"]]
    .map(([k, t]) => el("button", { type: "button", "aria-pressed": String(L.align === k), text: t,
      onclick: () => { L.align = k; renderCharts(); } })));

  // 1. cumulative R by month
  const cum = rs.map((r) => {
    let total = 0;
    const pts = new Map();
    for (const x of xs) {
      const k = monthKeyFor(r, x);
      const m = k && r.months[k];
      if (m && m.status === "COMPLETED" && m.sum_r !== null) { total += m.sum_r; pts.set(x, { y: total, tip: `${r2(total)} R (month ${r2(m.sum_r)} R, ${m.trades} trades${m.partial ? ", partial month" : ""})` }); }
      else if (m) pts.set(x, null);
    }
    return { slot: r.slot, label: r.label, pts };
  });
  box.append(card("Cumulative R by month", "Sum of R after costs, month by month. Each month of every study is a separate account; a gap means no data for that month.", [alignToggle,
    lineChart(cum, xs, width, { yFmt: (v) => r2(v), tickFmt: L.align === "aligned" ? (x) => x : shortMonth })]));

  // 2. monthly R heatmap (values printed in every cell)
  box.append(card("Monthly R", "Sum of R after costs in each month. Blue is positive, red negative; an asterisk marks a partial month.", [heatmap(rs, xs)]));

  // 3. cost stress
  const withCosts = rs.filter((r) => r.pooled.expectancy_r !== null && r.pooled.expectancy_r_costs_x2 !== null);
  const cx = ["×1", "×2", "×3"];
  const stress = withCosts.map((r) => ({ slot: r.slot, label: r.label, pts: new Map([["×1", r.pooled.expectancy_r], ["×2", r.pooled.expectancy_r_costs_x2], ["×3", r.pooled.expectancy_r_costs_x3]]
    .filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => [k, { y: v, tip: `${r3(v)} R/trade` }])) }));
  const missing = rs.length - withCosts.length;
  box.append(card("Execution stress", `Pooled R per trade with spread and slippage at their configured level, doubled and tripled.${missing ? ` ${missing} selected result${missing > 1 ? "s have" : " has"} no cost-stress figures (Phase 5 and 5.1 did not record them).` : ""}`,
    [withCosts.length ? lineChart(stress, cx, width, { yFmt: r3, tickFmt: (x) => `${x} costs`, markers: true }) : el("p", { class: "muted small", text: "None of the selected results has cost-stress figures." })]));

  // 4. regime breakdowns (Phase 6 results only)
  const kinds = [...new Set(rs.flatMap((r) => Object.keys(r.breakdowns || {})))];
  if (kinds.length) {
    box.append(card("Regime breakdown", "R per trade after costs by market condition at entry, followed by the trade count. Faded bars have fewer than 20 trades, too few to read much into. Only Phase 6 results record these breakdowns.",
      kinds.map((k) => groupedBars(k, rs.filter((r) => r.breakdowns && r.breakdowns[k])))));
  }
}

function card(title, note, children) {
  return el("section", { class: "lib-card" }, [el("h3", { text: title }), note ? el("p", { class: "small muted", text: note }) : null, ...children]);
}

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function shortMonth(k) { const [y, m] = k.split("-"); return `${MON[Number(m) - 1]} ${y.slice(2)}`; }

function niceTicks(lo, hi, n = 5) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const step0 = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((st) => st >= step0) || step0;
  const out = [];
  for (let v = Math.floor(lo / step) * step; v <= hi + step * 0.001; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

function tipLayer(wrap) {
  const tip = el("div", { class: "lib-tip", role: "status", hidden: true });
  wrap.append(tip);
  return {
    show(x, y, nodes) {
      clear(tip).append(...nodes);
      tip.hidden = false;
      const w = wrap.clientWidth;
      const tw = tip.offsetWidth;
      const th = tip.offsetHeight;
      tip.style.left = `${Math.min(Math.max(4, x + 14), Math.max(4, w - tw - 4))}px`;
      tip.style.top = `${y + th + 10 > wrap.clientHeight ? Math.max(0, y - th - 12) : Math.max(0, y - 10)}px`;   // flip above near the bottom
    },
    hide() { tip.hidden = true; },
  };
}

function lineChart(series, xs, width, { yFmt, tickFmt, markers = false }) {
  const direct = series.length <= 4 && width >= 560;
  const M = { l: 56, r: direct ? 150 : 16, t: 12, b: 30 };
  const H = 250;
  const pw = width - M.l - M.r;
  const ph = H - M.t - M.b;
  const ys = series.flatMap((sr) => [...sr.pts.values()].filter(Boolean).map((p) => p.y));
  if (!xs.length || !ys.length) return el("p", { class: "muted small", text: "No monthly data for the selected results." });
  const ticks = niceTicks(Math.min(0, ...ys), Math.max(0, ...ys));
  const lo = ticks[0];
  const hi = ticks[ticks.length - 1];
  const X = (i) => M.l + (xs.length === 1 ? pw / 2 : (i * pw) / (xs.length - 1));
  const Y = (v) => M.t + ph - ((v - lo) / (hi - lo || 1)) * ph;
  const root = s("svg", { viewBox: `0 0 ${width} ${H}`, width, height: H, class: "lib-svg", role: "img",
    "aria-label": `Line chart of ${series.map((x) => x.label).join(", ")}` });
  for (const t of ticks) {
    root.append(s("line", { x1: M.l, x2: M.l + pw, y1: Y(t), y2: Y(t), class: t === 0 ? "lib-zero" : "lib-grid" }),
      s("text", { x: M.l - 8, y: Y(t) + 4, "text-anchor": "end", class: "lib-axis" }, yFmt(t)));
  }
  const every = Math.max(1, Math.ceil(xs.length / Math.max(1, Math.floor(pw / 58))));
  const shown = xs.map((_, i) => i).filter((i) => i % every === 0);
  const lastI = xs.length - 1;
  if (shown[shown.length - 1] !== lastI) {          // always label the last month, dropping a neighbour that would collide
    if (lastI - shown[shown.length - 1] < every) shown.pop();
    shown.push(lastI);
  }
  for (const i of shown) root.append(s("text", { x: X(i), y: H - 10, "text-anchor": "middle", class: "lib-axis" }, tickFmt(xs[i])));
  const ends = [];
  for (const sr of series) {
    let d = "";
    let pen = false;
    let last = null;
    xs.forEach((x, i) => {
      const p = sr.pts.get(x);
      if (!p) { pen = false; return; }
      d += `${pen ? "L" : "M"}${X(i).toFixed(1)},${Y(p.y).toFixed(1)} `;
      pen = true;
      last = { i, p };
    });
    if (d) root.append(s("path", { d, class: `lib-line s${sr.slot}` }));
    const showAll = markers || xs.length <= 14;
    xs.forEach((x, i) => { const p = sr.pts.get(x); if (p && (showAll || (last && i === last.i))) root.append(s("circle", { cx: X(i), cy: Y(p.y), r: 4, class: `lib-dot s${sr.slot}` })); });
    if (last) ends.push({ y: Y(last.p.y), x: X(last.i), label: sr.label, slot: sr.slot });
  }
  if (direct) {                                       // direct labels, nudged apart so they never collide
    ends.sort((a, b) => a.y - b.y);
    for (let i = 1; i < ends.length; i += 1) if (ends[i].y - ends[i - 1].y < 14) ends[i].y = ends[i - 1].y + 14;
    for (const e of ends) root.append(s("text", { x: M.l + pw + 10, y: e.y + 4, class: "lib-dlabel" }, e.label.length > 22 ? `${e.label.slice(0, 21)}…` : e.label));
  }
  const cross = s("line", { y1: M.t, y2: M.t + ph, class: "lib-cross", visibility: "hidden" });
  const hit = s("rect", { x: M.l - 10, y: 0, width: pw + 20, height: H, class: "lib-hit" });
  root.append(cross, hit);
  const wrap = el("div", { class: "lib-chart" }, root);
  const tip = tipLayer(wrap);
  const move = (ev) => {
    const box = root.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * width;
    const i = xs.length === 1 ? 0 : Math.max(0, Math.min(xs.length - 1, Math.round(((px - M.l) / pw) * (xs.length - 1))));
    cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i)); cross.setAttribute("visibility", "visible");
    const rows = series.map((sr) => { const p = sr.pts.get(xs[i]); return p ? el("div", { class: "row" }, [swatch(sr.slot), el("span", { class: "lbl", text: sr.label }), el("span", { class: "num", text: p.tip })]) : null; }).filter(Boolean);
    tip.show((X(i) / width) * box.width, ((ev.clientY - box.top)), [el("div", { class: "head", text: tickFmt(xs[i]) }), ...(rows.length ? rows : [el("div", { class: "muted", text: "no data this month" })])]);
  };
  hit.addEventListener("mousemove", move);
  hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.hide(); });
  return wrap;
}

const NEUTRAL = [56, 56, 53];                         // diverging midpoint (dark), blue positive, red negative
const POS = [57, 135, 229];
const NEG = [230, 103, 103];
function heatColor(v, max) {
  const t = Math.min(1, Math.abs(v) / (max || 1));
  const step = t === 0 ? 0 : [0.3, 0.48, 0.66, 0.84, 1][Math.min(4, Math.floor(t * 5 - 1e-9))];
  const pole = v >= 0 ? POS : NEG;
  return `rgb(${NEUTRAL.map((c, i) => Math.round(c + (pole[i] - c) * step)).join(",")})`;
}

function heatmap(rs, xs) {
  const vals = rs.flatMap((r) => Object.values(r.months).map((m) => m.sum_r).filter((v) => v !== null && v !== undefined));
  const max = Math.max(1e-9, ...vals.map(Math.abs));
  const grid = el("div", { class: "lib-heat", role: "table", "aria-label": "Monthly R per result" });
  grid.style.gridTemplateColumns = `minmax(120px, 200px) repeat(${xs.length}, minmax(44px, 1fr))`;
  grid.append(el("div", { class: "hh", role: "columnheader" }), ...xs.map((x) => el("div", { class: "hh", role: "columnheader", text: L.align === "aligned" ? x : shortMonth(x) })));
  const wrap = el("div", { class: "lib-chart" }, el("div", { class: "lib-heat-wrap" }, grid));   // tip sits outside the scroller
  const tip = tipLayer(wrap);
  for (const r of rs) {
    grid.append(el("div", { class: "hl", role: "rowheader" }, [swatch(r.slot), el("span", { text: r.label })]));
    for (const x of xs) {
      const k = monthKeyFor(r, x);
      const m = k && r.months[k];
      if (!m) { grid.append(el("div", { class: "hc empty", role: "cell", "aria-label": "no data" })); continue; }
      if (m.status !== "COMPLETED" || m.sum_r === null) { grid.append(el("div", { class: "hc na", role: "cell", text: "n/a", title: m.status })); continue; }
      const c = el("div", { class: `hc${m.partial ? " partial" : ""}`, role: "cell", text: `${r2(m.sum_r)}${m.partial ? "*" : ""}` });
      c.style.backgroundColor = heatColor(m.sum_r, max);
      c.addEventListener("mouseenter", () => {
        const g = wrap.getBoundingClientRect(); const b = c.getBoundingClientRect();
        tip.show(b.left - g.left + b.width / 2, b.top - g.top, [el("div", { class: "head", text: `${r.label} · ${k}` }),
          el("div", { text: `${r2(m.sum_r)} R over ${m.trades} trades (${r3(m.expectancy_r)} R/trade)${m.partial ? " · partial month" : ""}` })]);
      });
      c.addEventListener("mouseleave", () => tip.hide());
      grid.append(c);
    }
  }
  return el("div", {}, [wrap, el("div", { class: "lib-heat-key small muted" }, [el("span", { text: `−${max.toFixed(1)} R` }),
    ...[-1, -0.6, -0.2, 0.2, 0.6, 1].map((f) => { const sw = el("span", { class: "key" }); sw.style.backgroundColor = heatColor(f * max, max); return sw; }),
    el("span", { text: `+${max.toFixed(1)} R` })])]);
}

function groupedBars(kind, rs) {
  const buckets = [...new Set(rs.flatMap((r) => Object.keys(r.breakdowns[kind])))];
  const vals = rs.flatMap((r) => Object.values(r.breakdowns[kind]).map((c) => c.expectancy_r).filter((v) => v !== null));
  const max = Math.max(1e-9, ...vals.map(Math.abs));
  const wrap = el("div", { class: "lib-chart lib-bars" });
  const tip = tipLayer(wrap);
  wrap.append(el("div", { class: "lib-bars-title small", text: kind }));
  for (const b of buckets) {
    const group = el("div", { class: "grp" }, el("div", { class: "glabel small", text: b }));
    for (const r of rs) {
      const c = r.breakdowns[kind][b];
      const v = c ? c.expectancy_r : null;
      const thin = c && c.trades !== null && c.trades < 20;
      const bar = el("span", { class: `bar s${r.slot}${v !== null && v < 0 ? " neg" : ""}${thin ? " thin" : ""}` });
      if (v !== null) { const w = (Math.abs(v) / max) * 50; bar.style.width = `${w}%`; bar.style.left = v >= 0 ? "50%" : `${50 - w}%`; }
      const row = el("div", { class: "brow" }, [el("span", { class: "track" }, [el("span", { class: "axis" }), bar]),
        el("span", { class: "num small" }, [el("span", { class: tone(v), text: v === null ? "—" : r3(v) }),
          el("span", { class: "muted", text: c && c.trades !== null ? ` · ${c.trades}` : "" })])]);
      row.addEventListener("mouseenter", () => {
        const g = wrap.getBoundingClientRect(); const bb = row.getBoundingClientRect();
        tip.show(bb.left - g.left + bb.width / 2, bb.top - g.top, [el("div", { class: "head", text: `${r.label} · ${b}` }),
          el("div", { text: c ? `${r3(v)} R/trade over ${c.trades} trades${c.trades !== null && c.trades < 20 ? " (thin sample)" : ""}` : "no trades" })]);
      });
      row.addEventListener("mouseleave", () => tip.hide());
      group.append(row);
    }
    wrap.append(group);
  }
  return wrap;
}

// ------------------------------------------------------------------ research reports (read-only markdown reader)
function renderDocs() {
  const box = document.getElementById("lib-docs");
  if (!box || !L.data) return;
  clear(box).append(L.docs.length ? el("ul", { class: "lib-doclist" }, L.docs.map((d) => el("li", {}, [
    el("button", { type: "button", class: "btn ghost lib-doc", onclick: () => openDoc(d.id) }, [el("span", { class: "t", text: d.title }),
      el("span", { class: "small muted", text: `${d.file} · ${(d.bytes / 1024).toFixed(0)} KB · ${d.modified.slice(0, 10)}` })])])))
    : el("p", { class: "muted small", text: "No reports found." }));
}

async function openDoc(id) {
  const reader = document.getElementById("lib-reader");
  clear(reader).append(el("p", { class: "muted small", text: "Opening…" }));
  reader.hidden = false;
  try {
    const d = await api(`/api/monthly/library/documents/${encodeURIComponent(id)}`, { timeoutMs: 30000 });
    clear(reader).append(el("div", { class: "lib-reader-head" }, [el("strong", { text: d.title }), el("span", { class: "small muted", text: d.file }),
      el("span", { class: "spacer" }), el("button", { type: "button", class: "btn ghost sm", text: "Close", onclick: () => { reader.hidden = true; clear(reader); } })]),
    el("article", { class: "md" }, markdown(d.text)));
    reader.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    clear(reader).append(el("p", { class: "notice error", text: `Could not open ${id}: ${err.message}` }));
  }
}

// Minimal, safe markdown: every node is built with textContent (no HTML from the document is ever interpreted).
function inline(text) {
  const out = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)\s]+\))/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    if (m[1]) out.push(el("code", { text: t.slice(1, -1) }));
    else if (m[2]) out.push(el("strong", {}, inline(t.slice(2, -2))));
    else if (m[3]) out.push(el("em", {}, inline(t.slice(1, -1))));
    else {
      const [, label, href] = t.match(/^\[([^\]]+)\]\(([^)\s]+)\)$/);
      const name = href.split("/").pop().split("#")[0];
      if (L.docs.some((d) => d.id === name)) out.push(el("a", { href: "#library", onclick: (e) => { e.preventDefault(); openDoc(name); } }, inline(label)));
      else if (safeHref(href) !== "#" && /^https:/.test(href)) out.push(el("a", { href, rel: "noopener noreferrer", target: "_blank" }, inline(label)));
      else out.push(...inline(label));
    }
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function markdown(src) {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const isTableSep = (l) => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "").split(/(?<!\\)\|/)    // an escaped pipe stays inside its cell
    .map((c) => c.trim().replace(/\\\|/g, "|"));
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*$/.test(line)) { i += 1; continue; }
    if (/^```/.test(line)) {
      const buf = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      i += 1;
      out.push(el("pre", {}, el("code", { text: buf.join("\n") })));
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { out.push(el(`h${Math.min(6, h[1].length + 1)}`, {}, inline(h[2]))); i += 1; continue; }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push(el("hr")); i += 1; continue; }
    if (/^\s*\|/.test(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { body.push(cells(lines[i])); i += 1; }
      out.push(el("div", { class: "table-wrap" }, el("table", { class: "compact data" }, [
        el("thead", {}, el("tr", {}, head.map((c) => el("th", { scope: "col" }, inline(c))))),
        el("tbody", {}, body.map((r) => el("tr", {}, r.map((c) => el("td", { class: /^[-+−]?[\d.,]+%?$/.test(c) ? "n" : null }, inline(c))))))])));
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, "")); i += 1; }
      out.push(el("blockquote", {}, inline(buf.join(" "))));
      continue;
    }
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) { const [node, next] = list(lines, i); out.push(node); i = next; continue; }
    const buf = [];
    while (i < lines.length && !/^\s*$/.test(lines[i]) && !/^(#{1,6}\s|```|\s*\||>|\s*([-*+]|\d+\.)\s+)/.test(lines[i])) { buf.push(lines[i].trim()); i += 1; }
    if (!buf.length) { buf.push(line.trim()); i += 1; }
    out.push(el("p", {}, inline(buf.join(" "))));
  }
  return out;
}

function list(lines, start) {
  const indent = (l) => l.match(/^\s*/)[0].length;
  const base = indent(lines[start]);
  const ordered = /^\s*\d+\./.test(lines[start]);
  const node = el(ordered ? "ol" : "ul");
  let i = start;
  let li = null;
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*$/.test(l)) { if (i + 1 < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i + 1]) && indent(lines[i + 1]) >= base) { i += 1; continue; } break; }
    const m = l.match(/^(\s*)([-*+]|\d+\.)\s+(.*)$/);
    if (m && indent(l) === base) { li = el("li", {}, inline(m[3])); node.append(li); i += 1; continue; }
    if (m && indent(l) > base && li) { const [sub, next] = list(lines, i); li.append(sub); i = next; continue; }
    if (!m && indent(l) > base && li) { li.append(" ", ...inline(l.trim())); i += 1; continue; }
    break;
  }
  return [node, i];
}
