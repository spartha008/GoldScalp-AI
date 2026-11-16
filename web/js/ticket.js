// MT5 DEMO order ticket on the Dashboard (GET/POST /api/live/demo/manual).
// Side, lots, stop-loss and take-profit; the SL and TP are lines on the candle chart that can be dragged. Before an
// order the chart shows the USD result and R at each line; after it, the open position's entry, SL and TP lines and
// its live P/L, with the SL/TP still draggable (applied on request) and a close button. Orders go only to the MT5 DEMO
// account: the server re-reads the account before every order and refuses anything else.
import { api } from "./api.js";
import { clear, el, fmtMoney, fmtPrice, fmtTime, num } from "./ui.js";

const URL = "/api/live/demo/manual";
const COL = { sl: "#e5484d", tp: "#2bb594", entry: "rgba(212,175,55,0.9)", pos: "#60a5fa" };
const GRAB_PX = 8;                                    // how close (CSS px) the pointer must be to grab a line
const T = {
  status: null, side: "buy", lots: 0.10, sl: null, tp: null, preview: true, quote: null,
  getChart: null, lines: new Map(), drag: null, pending: new Map(), bound: null, busy: false, frame: 0,
  plCells: new Map(), posSig: null,
};
const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ numbers
const digits = () => (T.status && T.status.symbol && T.status.symbol.digits) || 2;
const contract = () => (T.status && T.status.symbol && T.status.symbol.contract_size) || 100;
const round = (v) => Number(v.toFixed(digits()));

function prices() {
  if (T.quote && num(T.quote.bid) !== null && num(T.quote.ask) !== null) return { bid: num(T.quote.bid), ask: num(T.quote.ask) };
  const s = T.status && T.status.symbol;
  return s && s.available ? { bid: s.bid, ask: s.ask } : null;
}

/** A BUY fills at the ask and its SL/TP are hit by the bid; a SELL the other way round: the spread is included. */
function entryFor(side) {
  const p = prices();
  return p ? (side === "buy" ? p.ask : p.bid) : null;
}

function resultAt(side, entry, level, lots) {
  if (entry === null || level === null) return null;
  return (level - entry) * (side === "buy" ? 1 : -1) * contract() * lots;
}

function floating(p) {
  const q = prices();
  if (!q) return num(p.profit);
  return ((p.side === "buy" ? q.bid : q.ask) - p.price_open) * (p.side === "buy" ? 1 : -1) * contract() * p.volume;
}

function problemsFor(side, sl, tp) {
  const q = prices();
  const out = [];
  if (!q) return ["waiting for a price"];
  const mark = side === "buy" ? q.bid : q.ask;
  if (sl === null) out.push("set a stop-loss");
  else if (side === "buy" ? sl >= mark : sl <= mark) out.push(`a ${side.toUpperCase()} stop-loss must be ${side === "buy" ? "below the bid" : "above the ask"} ${fmtPrice(mark)}`);
  if (tp !== null && (side === "buy" ? tp <= mark : tp >= mark)) out.push(`a ${side.toUpperCase()} take-profit must be ${side === "buy" ? "above the bid" : "below the ask"} ${fmtPrice(mark)}`);
  return out;
}

// ------------------------------------------------------------------ chart lines
function series() {
  const c = T.getChart && T.getChart();
  return c ? c.series : null;
}

function setLine(key, opts) {
  const s = series();
  if (!s) return;
  const line = T.lines.get(key);
  if (line) line.applyOptions(opts);
  else T.lines.set(key, s.createPriceLine({ lineWidth: 2, axisLabelVisible: true, ...opts }));
}

function dropLines(keep) {
  const s = series();
  for (const [key, line] of T.lines) {
    if (!keep.has(key)) { if (s) s.removePriceLine(line); T.lines.delete(key); }
  }
}

const usdR = (usd, r) => `${fmtMoney(usd, { sign: true })}${r === null ? "" : ` (${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(2)}R)`}`;

function draw() {
  T.frame = 0;
  bindChart();                                        // the chart is created after the ticket
  const keep = new Set();
  const s = series();
  if (!s) return;
  // before the order: the planned entry, SL and TP with their results
  const entry = entryFor(T.side);
  if (T.preview && entry !== null && T.sl !== null) {
    const risk = Math.abs(entry - T.sl);
    const atSl = resultAt(T.side, entry, T.sl, T.lots);
    setLine("p-entry", { price: entry, color: COL.entry, lineWidth: 1, lineStyle: 2, title: `${T.side.toUpperCase()} ${T.lots.toFixed(2)} (planned)` });
    setLine("p-sl", { price: T.sl, color: COL.sl, lineStyle: 0, title: `SL ${usdR(atSl, risk ? -1 : null)} · drag` });
    keep.add("p-entry").add("p-sl");
    if (T.tp !== null) {
      const atTp = resultAt(T.side, entry, T.tp, T.lots);
      setLine("p-tp", { price: T.tp, color: COL.tp, lineStyle: 0, title: `TP ${usdR(atTp, risk ? Math.abs(T.tp - entry) / risk : null)} · drag` });
      keep.add("p-tp");
    }
  }
  // after the order: every open manual position with its live P/L
  for (const p of (T.status && T.status.positions) || []) {
    const edit = T.pending.get(p.ticket) || {};
    const sl = edit.sl ?? (p.sl || null);
    const tp = edit.tp ?? (p.tp || null);
    const risk = sl ? Math.abs(p.price_open - sl) : 0;
    const k = `t${p.ticket}`;
    setLine(`${k}-entry`, { price: p.price_open, color: COL.pos, lineStyle: 0,
      title: `#${p.ticket} ${p.side.toUpperCase()} ${Number(p.volume).toFixed(2)} · P/L ${fmtMoney(floating(p), { sign: true })}` });
    keep.add(`${k}-entry`);
    if (sl) {
      setLine(`${k}-sl`, { price: sl, color: COL.sl, lineStyle: edit.sl !== undefined ? 2 : 1,
        title: `#${p.ticket} SL ${usdR(resultAt(p.side, p.price_open, sl, p.volume), risk ? -1 : null)}${edit.sl !== undefined ? " (not applied)" : ""}` });
      keep.add(`${k}-sl`);
    }
    if (tp) {
      setLine(`${k}-tp`, { price: tp, color: COL.tp, lineStyle: edit.tp !== undefined ? 2 : 1,
        title: `#${p.ticket} TP ${usdR(resultAt(p.side, p.price_open, tp, p.volume), risk ? Math.abs(tp - p.price_open) / risk : null)}${edit.tp !== undefined ? " (not applied)" : ""}` });
      keep.add(`${k}-tp`);
    }
  }
  dropLines(keep);
}

const redraw = () => { if (!T.frame) T.frame = requestAnimationFrame(draw); };

// ------------------------------------------------------------------ dragging the lines
function draggable() {
  const out = [];
  if (T.preview && T.sl !== null) out.push(["p-sl", T.sl]);
  if (T.preview && T.tp !== null) out.push(["p-tp", T.tp]);
  for (const p of (T.status && T.status.positions) || []) {
    const edit = T.pending.get(p.ticket) || {};
    const sl = edit.sl ?? (p.sl || null);
    const tp = edit.tp ?? (p.tp || null);
    if (sl) out.push([`t${p.ticket}-sl`, sl]);
    if (tp) out.push([`t${p.ticket}-tp`, tp]);
  }
  return out;
}

function hit(y) {
  const s = series();
  if (!s) return null;
  let best = null;
  for (const [key, price] of draggable()) {
    const yy = s.priceToCoordinate(price);
    if (yy !== null && Math.abs(yy - y) <= GRAB_PX && (!best || Math.abs(yy - y) < best.d)) best = { key, d: Math.abs(yy - y) };
  }
  return best && best.key;
}

function moveTo(key, price) {
  if (key === "p-sl") { T.sl = price; $("tk-sl").value = price.toFixed(digits()); }
  else if (key === "p-tp") { T.tp = price; $("tk-tp").value = price.toFixed(digits()); }
  else {
    const [, ticket, which] = key.match(/^t(\d+)-(sl|tp)$/);
    const edit = { ...(T.pending.get(Number(ticket)) || {}) };
    edit[which] = price;
    T.pending.set(Number(ticket), edit);
    renderPositions();
  }
  renderEstimate();
  redraw();
}

function bindChart() {
  const c = T.getChart && T.getChart();
  const box = $("chart");
  if (!c || !box || T.bound === c) return;
  T.bound = c;
  const y = (e) => e.clientY - box.getBoundingClientRect().top;
  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  box.addEventListener("pointerdown", (e) => {
    const key = hit(y(e));
    if (!key) return;
    stop(e);
    T.drag = { key, id: e.pointerId };
    c.chart.applyOptions({ handleScroll: false, handleScale: false });
    try { box.setPointerCapture(e.pointerId); } catch (_) { /* older browsers */ }
  }, { capture: true });
  box.addEventListener("pointermove", (e) => {
    if (!T.drag) { box.classList.toggle("ticket-grab", Boolean(hit(y(e)))); return; }
    stop(e);
    const price = c.series.coordinateToPrice(y(e));
    if (price !== null && Number.isFinite(price)) moveTo(T.drag.key, round(price));
  }, { capture: true });
  const end = (e) => {
    if (!T.drag) return;
    try { box.releasePointerCapture(T.drag.id); } catch (_) { /* already released */ }
    T.drag = null;
    c.chart.applyOptions({ handleScroll: true, handleScale: true });
    if (e) stop(e);
  };
  box.addEventListener("pointerup", end, { capture: true });
  box.addEventListener("pointercancel", end, { capture: true });
  box.addEventListener("mousedown", (e) => { if (T.drag) stop(e); }, { capture: true });
  redraw();
}

// ------------------------------------------------------------------ the panel
function stat(label, value, cls = "") {
  return el("div", { class: "stat" }, [el("div", { class: "label", text: label }), el("div", { class: `value num ${cls}`.trim(), text: value })]);
}

function readLevels() {
  T.lots = Math.max(0, num($("tk-lots").value) ?? 0);
  T.sl = num($("tk-sl").value);
  T.tp = num($("tk-tp").value);
}

function defaults() {
  const entry = entryFor(T.side);
  if (entry === null) return;
  const d = 5;                                             // $5 stop, 2R target: a starting point to drag from
  T.sl = round(T.side === "buy" ? entry - d : entry + d);
  T.tp = round(T.side === "buy" ? entry + 2 * d : entry - 2 * d);
  $("tk-sl").value = T.sl.toFixed(digits());
  $("tk-tp").value = T.tp.toFixed(digits());
}

function blockers() {
  const s = T.status;
  if (!s) return ["loading the MT5 demo account"];
  const out = [...(s.problems || [])];
  if (!s.enabled) return out;
  const max = num(s.limits && s.limits.max_lots);
  if (!(T.lots >= 0.01) || Math.abs(T.lots * 100 - Math.round(T.lots * 100)) > 1e-6) out.push("lots must be at least 0.01, in steps of 0.01");
  else if (max !== null && T.lots > max) out.push(`${T.lots.toFixed(2)} lots is above the demo limit of ${max.toFixed(2)} lots`);
  return out.concat(problemsFor(T.side, T.sl, T.tp));
}

function renderEstimate() {
  const box = $("tk-est");
  const entry = entryFor(T.side);
  const risk = entry !== null && T.sl !== null ? Math.abs(entry - T.sl) : null;
  const atSl = resultAt(T.side, entry, T.sl, T.lots);
  const atTp = resultAt(T.side, entry, T.tp, T.lots);
  const bal = num(T.status && T.status.account && T.status.account.balance);
  clear(box).append(
    stat(`Entry (${T.side === "buy" ? "ask" : "bid"}, market)`, entry === null ? "—" : fmtPrice(entry, digits())),
    stat("At stop-loss", atSl === null ? "—" : fmtMoney(atSl, { sign: true }), "trend-down"),
    stat("At take-profit", atTp === null ? "—" : `${fmtMoney(atTp, { sign: true })}${risk ? ` · ${(Math.abs(T.tp - entry) / risk).toFixed(2)}R` : ""}`, atTp === null ? "" : "trend-up"),
    stat("Risk of balance", atSl === null || !bal ? "—" : `${(Math.abs(atSl) / bal * 100).toFixed(2)}%`));
  const why = blockers();
  $("tk-review").disabled = T.busy || why.length > 0;
  const note = $("tk-why");
  note.hidden = !why.length;
  note.textContent = why.length ? `Not ready: ${why.join("; ")}.` : "";
}

function renderAccount() {
  const s = T.status;
  const dl = clear($("tk-acct"));
  const probs = $("tk-problems");
  if (!s) return;
  const a = s.account || {};
  const pairs = [["Account", s.enabled ? (a.demo ? `DEMO · ${a.server || ""}` : "not a demo account") : "demo execution off"],
    ["Balance", a.balance === undefined || a.balance === null ? "—" : fmtMoney(a.balance)],
    ["Equity", a.equity === undefined || a.equity === null ? "—" : fmtMoney(a.equity)],
    ["Lot limit", s.limits ? `${Number(s.limits.max_lots).toFixed(2)} per order` : "—"]];
  for (const [k, v] of pairs) dl.append(el("dt", { text: k }), el("dd", { class: "num", text: v }));
  const list = s.problems || [];
  probs.hidden = !list.length;
  probs.textContent = list.length ? `Orders cannot be sent now: ${list.join("; ")}.` : "";
}

function updatePL() {
  for (const p of (T.status && T.status.positions) || []) {
    const td = T.plCells.get(p.ticket);
    if (!td) continue;
    const pl = floating(p);
    td.textContent = fmtMoney(pl, { sign: true });
    td.className = `num ${pl > 0 ? "trend-up" : pl < 0 ? "trend-down" : ""}`.trim();
  }
}

function renderPositions(force = false) {
  const list = (T.status && T.status.positions) || [];
  // rebuild only when something other than the price changed, so a button is never replaced under the pointer
  const sig = JSON.stringify([list.map((p) => [p.ticket, p.sl, p.tp, p.volume]), [...T.pending]]);
  if (!force && sig === T.posSig) { updatePL(); return; }
  T.posSig = sig;
  T.plCells = new Map();
  const box = clear($("tk-positions"));
  if (!list.length) { box.append(el("p", { class: "muted small", text: "No open manual positions." })); return; }
  const rows = list.map((p) => {
    const edit = T.pending.get(p.ticket);
    const pl = floating(p);
    const plCell = el("td", { class: `num ${pl > 0 ? "trend-up" : pl < 0 ? "trend-down" : ""}`.trim(), text: fmtMoney(pl, { sign: true }),
      title: `broker P/L ${fmtMoney(p.profit, { sign: true })} (includes the broker's own costs)` });
    T.plCells.set(p.ticket, plCell);
    return el("tr", {}, [
      el("td", { class: "num", text: `#${p.ticket}` }),
      el("td", { class: p.side === "buy" ? "trend-up" : "trend-down", text: p.side.toUpperCase() }),
      el("td", { class: "num", text: Number(p.volume).toFixed(2) }),
      el("td", { class: "num", text: fmtPrice(p.price_open, digits()) }),
      el("td", { class: "num", text: fmtPrice(edit && edit.sl !== undefined ? edit.sl : p.sl || null, digits()) }),
      el("td", { class: "num", text: fmtPrice(edit && edit.tp !== undefined ? edit.tp : p.tp || null, digits()) }),
      plCell,
      el("td", {}, [
        edit ? el("button", { type: "button", class: "btn sm primary", text: "Apply SL/TP", onclick: () => applyEdit(p) }) : null,
        edit ? el("button", { type: "button", class: "btn sm ghost", text: "Undo", onclick: () => { T.pending.delete(p.ticket); renderPositions(); redraw(); } }) : null,
        el("button", { type: "button", class: "btn sm", text: "Close", onclick: () => closeOne(p) })])]);
  });
  box.append(el("div", { class: "table-wrap" }, el("table", { class: "compact data" }, [
    el("thead", {}, el("tr", {}, ["Ticket", "Side", "Lots", "Open", "SL", "TP", "P/L", ""].map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, rows)])));
}

function renderOrders() {
  const box = clear($("tk-orders"));
  const list = ((T.status && T.status.orders) || []).slice(0, 8);
  if (!list.length) { box.append(el("p", { class: "muted small", text: "No ticket orders yet." })); return; }
  box.append(el("ul", { class: "plain small" }, list.map((o) => el("li", {}, [
    el("span", { class: "muted", text: `${fmtTime(o.created_at, true)} · ` }),
    `${o.action}${o.side ? ` ${o.side.toUpperCase()}` : ""}${o.volume ? ` ${o.volume}` : ""}${o.position_ticket ? ` #${o.position_ticket}` : ""}: `,
    el("strong", { class: o.status === "done" ? "trend-up" : o.status === "pending" ? "" : "trend-down", text: o.status }),
    o.message ? ` (${o.message})` : ""]))));
}

function say(text, tone = "") {
  const n = $("tk-result");
  n.className = `notice ${tone}`.trim();
  n.hidden = !text;
  n.textContent = text || "";
}

function confirmBox(title, lines, okLabel) {
  const dlg = $("tk-dialog");
  $("tk-dialog-title").textContent = title;
  const dl = el("dl", { class: "kv" }, lines.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { class: "num", text: v })]));
  clear($("tk-dialog-body")).append(dl, el("p", { class: "muted small", text: "MT5 DEMO account only (virtual funds). Estimates include the spread, not the broker's commission or swap; the fill price can differ." }));
  $("tk-dialog-ok").textContent = okLabel;
  return new Promise((resolve) => {
    const done = (ok) => { dlg.close(); $("tk-dialog-ok").onclick = null; $("tk-dialog-cancel").onclick = null; dlg.oncancel = null; resolve(ok); };
    $("tk-dialog-ok").onclick = () => done(true);
    $("tk-dialog-cancel").onclick = () => done(false);
    dlg.oncancel = () => done(false);
    dlg.showModal();
  });
}

async function review() {
  readLevels();
  if (blockers().length) { renderEstimate(); return; }
  const entry = entryFor(T.side);
  const risk = Math.abs(entry - T.sl);
  const a = T.status.account || {};
  const lines = [["Account", `DEMO · ${a.server || ""}`], ["Order", `${T.side.toUpperCase()} ${T.lots.toFixed(2)} lots, market`],
    ["Entry now", `${fmtPrice(entry, digits())} (${T.side === "buy" ? "ask" : "bid"})`],
    ["Stop-loss", `${fmtPrice(T.sl, digits())} → ${fmtMoney(resultAt(T.side, entry, T.sl, T.lots), { sign: true })}`],
    ["Take-profit", T.tp === null ? "none" : `${fmtPrice(T.tp, digits())} → ${fmtMoney(resultAt(T.side, entry, T.tp, T.lots), { sign: true })} (${(Math.abs(T.tp - entry) / risk).toFixed(2)}R)`]];
  if (!(await confirmBox("Send this order to your MT5 demo account?", lines, `Send ${T.side.toUpperCase()} ${T.lots.toFixed(2)} to MT5 demo`))) return;
  T.busy = true;
  renderEstimate();
  try {
    const out = await api(`${URL}/orders`, { method: "POST", timeoutMs: 30000, body: {
      side: T.side, volume: T.lots.toFixed(2), sl: T.sl, tp: T.tp, confirm: "DEMO", account_hash: a.account_hash } });
    const o = out.order || {};
    say(out.ok ? `Sent: ${T.side.toUpperCase()} ${T.lots.toFixed(2)} lots, position #${o.position_ticket}${o.price ? ` filled at ${fmtPrice(o.price, digits())}` : ""}.`
      : `The terminal rejected the order: ${out.message}`, out.ok ? "ok" : "error");
  } catch (err) {
    say(`Not sent: ${err.message}`, "error");
  } finally {
    T.busy = false;
    await refresh();
  }
}

async function applyEdit(p) {
  const edit = T.pending.get(p.ticket) || {};
  const sl = edit.sl ?? (p.sl || null);
  const tp = edit.tp ?? (p.tp || null);
  const ok = await confirmBox(`Change #${p.ticket}'s stop-loss and take-profit?`, [
    ["Position", `${p.side.toUpperCase()} ${Number(p.volume).toFixed(2)} @ ${fmtPrice(p.price_open, digits())}`],
    ["Stop-loss", `${fmtPrice(sl, digits())} → ${fmtMoney(resultAt(p.side, p.price_open, sl, p.volume), { sign: true })}`],
    ["Take-profit", tp ? `${fmtPrice(tp, digits())} → ${fmtMoney(resultAt(p.side, p.price_open, tp, p.volume), { sign: true })}` : "none"]], "Apply on MT5 demo");
  if (!ok) return;
  try {
    const out = await api(`${URL}/positions/${p.ticket}/protection`, { method: "POST", timeoutMs: 30000,
      body: { sl, tp, account_hash: T.status.account && T.status.account.account_hash } });
    say(out.ok ? `#${p.ticket}: stop-loss and take-profit changed.` : `The terminal rejected the change: ${out.message}`, out.ok ? "ok" : "error");
    if (out.ok) T.pending.delete(p.ticket);
  } catch (err) {
    say(`Not changed: ${err.message}`, "error");
  }
  await refresh();
}

async function closeOne(p) {
  const ok = await confirmBox(`Close #${p.ticket} at market?`, [
    ["Position", `${p.side.toUpperCase()} ${Number(p.volume).toFixed(2)} @ ${fmtPrice(p.price_open, digits())}`],
    ["P/L now", fmtMoney(floating(p), { sign: true })]], "Close on MT5 demo");
  if (!ok) return;
  try {
    const out = await api(`${URL}/positions/${p.ticket}/close`, { method: "POST", timeoutMs: 30000,
      body: { account_hash: T.status.account && T.status.account.account_hash } });
    say(out.ok ? `#${p.ticket} closed.` : `The terminal rejected the close: ${out.message}`, out.ok ? "ok" : "error");
    if (out.ok) T.pending.delete(p.ticket);
  } catch (err) {
    say(`Not closed: ${err.message}`, "error");
  }
  await refresh();
}

async function refresh() {
  try {
    T.status = await api(URL, { timeoutMs: 15000 });
  } catch (err) {
    T.status = { enabled: false, problems: [`the ticket could not be loaded: ${err.message}`], positions: [], orders: [] };
  }
  const open = new Set(((T.status && T.status.positions) || []).map((p) => p.ticket));
  for (const t of [...T.pending.keys()]) if (!open.has(t)) T.pending.delete(t);
  if (T.sl === null && T.tp === null) defaults();
  bindChart();
  renderAccount();
  renderEstimate();
  if (!T.drag) renderPositions();
  renderOrders();
  redraw();
}

function setSide(side) {
  T.side = side;
  $("tk-buy").setAttribute("aria-pressed", String(side === "buy"));
  $("tk-sell").setAttribute("aria-pressed", String(side === "sell"));
  defaults();
  renderEstimate();
  redraw();
}

/** Called by the dashboard with every market snapshot (the same quotes the chart uses). */
export function ticketQuote(snap) {
  if (!snap || !snap.quote) return;
  T.quote = snap.quote;
  if (T.sl === null && T.tp === null && T.status) defaults();
  if (!T.drag) {
    renderEstimate();
    updatePL();
  }
  redraw();
}

export function initTicket(getChart) {
  if (!$("ticket-panel")) return;
  T.getChart = getChart;
  $("tk-buy").addEventListener("click", () => setSide("buy"));
  $("tk-sell").addEventListener("click", () => setSide("sell"));
  for (const id of ["tk-lots", "tk-sl", "tk-tp"]) $(id).addEventListener("input", () => { readLevels(); renderEstimate(); redraw(); });
  $("tk-preview").addEventListener("change", (e) => { T.preview = e.target.checked; redraw(); });
  $("tk-reset").addEventListener("click", () => { defaults(); renderEstimate(); redraw(); });
  $("tk-review").addEventListener("click", review);
  refresh();
  setInterval(() => { if (!document.hidden && !T.drag) refresh(); }, 3000);
}
