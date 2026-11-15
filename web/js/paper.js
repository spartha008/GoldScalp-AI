// Paper Trading page. Everything here is SIMULATED.
import { api } from "./api.js";
import { createLineChart } from "./chart.js";
import { subscribeQuotes } from "./market-stream.js";
import {
  announce, clear, el, fmtLots, fmtMoney, fmtPrice, fmtTime, kindBadge, num, pnlClass, setDisplayZone, setText,
  stateBadge,
} from "./ui.js";

const state = { side: "buy", spec: null, costs: null, equityChart: null, estimateTimer: null, busy: false,
                brokerSpec: null };

// ------------------------------------------------------------------ account + spec

async function refreshAccount() {
  let env;
  try { env = await api("/api/sim/account"); } catch (err) { setText("s-note", `Could not load the account: ${err.message}`); return; }
  const { summary: s, instrument, costs } = env.data;
  state.spec = instrument; state.costs = costs;
  setText("sum-name", `${s.config.name} · #${s.account_id} · leverage 1:${s.config.leverage}`);
  setText("s-balance", fmtMoney(s.balance));
  setText("s-equity", s.equity !== null ? fmtMoney(s.equity) : "unknown (no price)");
  setText("s-used", fmtMoney(s.used_margin));
  setText("s-free", s.free_margin !== null ? fmtMoney(s.free_margin) : "unknown");
  setText("s-level", s.margin_level_pct !== null ? `${num(s.margin_level_pct).toFixed(1)}%` : "n/a (no margin used)");
  paint("s-upl", s.unrealized_pnl, true);
  paint("s-rpl", s.realized_pnl_net, true);
  paint("s-net", s.net_change, true);
  const m = s.mark;
  setText("sum-mark", m ? `P/L marked at ${m.price_state} price ${fmtTime(m.quote_time)} (${m.spread_source} spread)` : "no price to mark positions");
  setText("s-note", `${s.label}. Commission already paid on open positions: ${fmtMoney(s.open_commission_paid)}.`
    + (s.mark_note ? ` Note: ${s.mark_note}.` : ""));
  renderSpec(instrument, costs, s.config);
  if (!document.getElementById("presets").childElementCount) renderPresets(instrument);
  updateOunces();
}

function paint(id, value, sign) {
  const node = document.getElementById(id);
  node.textContent = value === null || value === undefined ? "unknown" : fmtMoney(value, { sign });
  const c = pnlClass(value);
  node.className = `value num ${c ? (c === "up" ? "trend-up" : "trend-down") : ""}`;
}

function renderSpec(spec, costs, config) {
  let origin = spec.is_hypothetical ? `Hypothetical specification: ${spec.spec_origin}` : spec.spec_origin;
  const broker = state.brokerSpec;
  if (broker && num(broker.contract_size) !== num(spec.contract_size)) {
    origin += ` WARNING: your MT5 symbol ${broker.name} uses ${broker.contract_size} units per lot, but this simulator `
      + `uses ${spec.contract_size}. Update the instrument file to match your broker before comparing results.`;
  } else if (broker) {
    origin += ` Your MT5 symbol ${broker.name} also uses ${broker.contract_size} units per lot.`;
  }
  setText("spec-origin", origin);
  const rows = [
    ["Symbol", spec.symbol], ["Contract size", `${spec.contract_size} troy oz per 1.00 lot`],
    ["Lot limits", `min ${spec.min_lot} · step ${spec.lot_step} · max ${spec.max_lot}`],
    ["Lot presets", Object.entries(spec.lot_presets).map(([k, v]) => `${k} ${v}`).join(" · ")],
    ["Tick size", `${spec.tick_size} USD`],
    ["Trading hours", `${spec.schedule.timezone}: daily close ${spec.schedule.daily_close}, reopen ${spec.schedule.daily_reopen}; weekend closed`],
    ["Spread source", { market: "provider bid/ask only", configured: "configured spread", market_else_configured: "provider bid/ask when available, else configured" }[costs.spread_mode]],
    ["Configured spread", costs.configured_spread ? `${costs.configured_spread} USD/oz` : "n/a"],
    ["Commission", `${costs.commission_per_lot_per_side} USD per lot per side`],
    ["Slippage", `${costs.slippage} USD/oz per fill (always adverse)`],
    ["Leverage", `1:${config.leverage}`], ["Max price age for fills", `${config.max_quote_age_seconds} s`],
    ["Fills on DELAYED prices", config.allow_delayed_fills ? "allowed (labelled)" : "refused"],
  ];
  const dl = clear(document.getElementById("spec"));
  for (const [k, v] of rows) dl.append(el("dt", { text: k }), el("dd", { text: v }));
}

function renderPresets(spec) {
  const box = document.getElementById("presets");
  const labels = { standard: "Standard", mini: "Mini", micro: "Micro" };
  const presets = Object.entries(spec.lot_presets).sort((a, b) => num(b[1]) - num(a[1]));
  for (const [name, lots] of presets) {
    box.append(el("button", { type: "button", class: "btn", "data-lots": lots,
                              text: `${labels[name] || name} ${fmtLots(lots)}`,
                              onclick: () => { document.getElementById("lots").value = fmtLots(lots); updateOunces(); scheduleEstimate(); } }));
  }
  box.append(el("button", { type: "button", class: "btn", text: "Custom", onclick: () => document.getElementById("lots").focus() }));
}

function updateOunces() {
  const lots = num(document.getElementById("lots").value);
  const size = state.spec ? num(state.spec.contract_size) : null;
  if (lots === null || size === null) { setText("oz", null); return; }
  setText("oz", `${(lots * size).toLocaleString("en-US", { maximumFractionDigits: 4 })} troy oz`);
  setText("oz-note", `${fmtLots(lots)} lots × ${state.spec.contract_size} oz per lot (from the configured instrument specification).`);
}

// ------------------------------------------------------------------ ticket

function scheduleEstimate() {
  clearTimeout(state.estimateTimer);
  state.estimateTimer = setTimeout(estimate, 250);
}

async function estimate() {
  const dl = clear(document.getElementById("est"));
  const lots = document.getElementById("lots").value.trim();
  if (!lots) return;
  let env;
  try { env = await api("/api/sim/estimate", { method: "POST", body: { side: state.side, lots } }); }
  catch (err) { dl.append(el("dt", { text: "Estimate" }), el("dd", { text: err.message })); setSubmit(false, err.message); return; }
  const e = env.data;
  const rows = [["Quantity", `${e.ounces} oz`]];
  if (e.fill_price) {
    rows.push(["Est. fill price", `${fmtPrice(e.fill_price, 3)} (${e.price_state} price from ${e.source_id})`],
              ["Bid / ask", `${fmtPrice(e.bid, 3)} / ${fmtPrice(e.ask, 3)}`],
              ["Spread", `${fmtPrice(e.spread, 2)} USD/oz (${e.spread_source === "market" ? "from provider" : "configured"})`],
              ["Notional", fmtMoney(e.notional)], ["Margin required", `${fmtMoney(e.margin_required)} at 1:${e.leverage}`],
              ["Open commission", fmtMoney(e.open_commission)],
              ["Round-trip cost (no move)", `${fmtMoney(e.round_trip.total)} = spread ${fmtMoney(e.round_trip.spread_cost)} + commission ${fmtMoney(e.round_trip.commission)} + slippage ${fmtMoney(e.round_trip.slippage_cost)}`],
              ["Free margin now", fmtMoney(e.free_margin)]);
  }
  for (const [k, v] of rows) dl.append(el("dt", { text: k }), el("dd", { text: v }));
  setSubmit(e.accepted, e.reason);
}

function setSubmit(ok, reason) {
  const btn = document.getElementById("submit");
  btn.disabled = !ok || state.busy;
  const msg = document.getElementById("t-msg");
  if (!ok && reason) announce(msg, `Order would be refused: ${reason}`, "warn");
  else if (msg.classList.contains("warn")) announce(msg, "");
}

async function submit() {
  if (state.busy) return;
  state.busy = true;
  const msg = document.getElementById("t-msg");
  const body = { side: state.side, lots: document.getElementById("lots").value.trim(),
                 stop_loss: document.getElementById("sl").value.trim() || null,
                 take_profit: document.getElementById("tp").value.trim() || null };
  if (state.signalId) body.signal_id = state.signalId;
  try {
    const env = await api("/api/sim/orders", { method: "POST", body });
    const p = env.data.position;
    announce(msg, `Simulated ${p.side.toUpperCase()} ${p.lots} lots (${p.ounces} oz) opened at ${fmtPrice(p.open_price, 3)} on a ${p.open_price_state} price. Commission ${fmtMoney(p.open_commission)}.`, "ok");
    document.getElementById("sl").value = ""; document.getElementById("tp").value = "";
    if (state.signalId) { state.signalId = null; document.getElementById("sig-prefill").hidden = true; }
  } catch (err) {
    announce(msg, `Order refused: ${err.message}`, "error");
  } finally {
    state.busy = false;
    await refreshAll();
  }
}

// ------------------------------------------------------------------ positions

async function refreshPositions() {
  let openEnv, closedEnv;
  try {
    [openEnv, closedEnv] = await Promise.all([api("/api/sim/positions?status=open"), api("/api/sim/positions?status=closed")]);
  } catch (_) { return; }
  const open = openEnv.data, closed = closedEnv.data;
  setText("open-count", `${open.length} open`);
  renderOpen(open);
  const cb = clear(document.getElementById("closed-body"));
  if (!closed.length) cb.append(el("tr", { class: "empty" }, [el("td", { colspan: "10", text: "No closed simulated positions yet." })]));
  let wins = 0, total = 0;
  for (const p of closed) {
    const commission = num(p.open_commission) + num(p.close_commission);
    if (num(p.realized_net) > 0) wins += 1;
    total += num(p.realized_net) || 0;
    cb.append(el("tr", {}, [
      el("td", { text: p.id }), el("td", { class: p.side === "buy" ? "up" : "down", text: p.side.toUpperCase() }),
      el("td", { class: "num", text: fmtLots(p.lots) }), el("td", { class: "num", text: fmtPrice(p.open_price, 3) }),
      el("td", { class: "num", text: fmtPrice(p.close_price, 3), title: `priced by ${p.close_source || "unknown source"}` }),
      el("td", { text: (p.close_reason || "").replace("_", " ") }),
      el("td", { class: `num ${pnlClass(p.realized_gross)}`, text: fmtMoney(p.realized_gross, { sign: true }) }),
      el("td", { class: "num", text: fmtMoney(commission) }),
      el("td", { class: `num ${pnlClass(p.realized_net)}`, text: fmtMoney(p.realized_net, { sign: true }) }),
      el("td", { text: fmtTime(p.close_time, true) }),
    ]));
  }
  setText("closed-stats", closed.length ? `${closed.length} closed · ${wins} winning · net ${fmtMoney(total, { sign: true })}` : "");
}

// Rows are keyed by position id and updated IN PLACE, so buttons stay clickable and typed SL/TP values survive
// refreshes. The table is rebuilt only when positions are added or removed.
const openRows = new Map();

function renderOpen(open) {
  const body = document.getElementById("open-body");
  const ids = open.map((p) => p.id).join(",");
  if (body.dataset.ids !== ids) {
    body.dataset.ids = ids;
    clear(body);
    openRows.clear();
    if (!open.length) body.append(el("tr", { class: "empty" }, [el("td", { colspan: "11", text: "No open simulated positions." })]));
    for (const p of open) {
      const slIn = el("input", { type: "text", value: p.stop_loss || "", placeholder: "SL", "aria-label": `Stop loss for position ${p.id}` });
      const tpIn = el("input", { type: "text", value: p.take_profit || "", placeholder: "TP", "aria-label": `Take profit for position ${p.id}` });
      const save = el("button", { type: "button", class: "btn", text: "Set", onclick: () => protect(p.id, slIn.value, tpIn.value) });
      const mark = el("td", { class: "num" });
      const upl = el("td", { class: "num" });
      body.append(el("tr", {}, [
        el("td", { text: p.id }), el("td", { class: p.side === "buy" ? "up" : "down", text: p.side.toUpperCase() }),
        el("td", { class: "num", text: fmtLots(p.lots) }), el("td", { class: "num", text: num(p.ounces) }),
        el("td", { class: "num", text: fmtPrice(p.open_price, 3), title: `filled on a ${p.open_price_state} price from ${p.open_source || "unknown source"}, spread ${p.open_spread} (${p.open_spread_source})${p.signal_id ? `; ticket prefilled from signal ${p.signal_id}` : ""}` }),
        mark, upl, el("td", { class: "num", text: fmtMoney(p.margin) }),
        el("td", {}, [el("span", { class: "inline-edit" }, [slIn, tpIn, save])]),
        el("td", { text: fmtTime(p.open_time, true) }),
        el("td", {}, [el("button", { type: "button", class: "btn", text: "Close", "aria-label": `Close simulated position ${p.id}`, onclick: () => closePosition(p.id) })]),
      ]));
      openRows.set(p.id, { mark, upl });
    }
  }
  for (const p of open) {
    const row = openRows.get(p.id);
    if (!row) continue;
    row.mark.textContent = p.mark_price ? fmtPrice(p.mark_price, 3) : "—";
    row.mark.title = p.mark_state ? `marked at a ${p.mark_state} price` : "";
    row.upl.textContent = p.unrealized_pnl !== undefined ? fmtMoney(p.unrealized_pnl, { sign: true }) : "unknown";
    row.upl.className = `num ${pnlClass(p.unrealized_pnl)}`;
  }
}

async function closePosition(id) {
  const msg = document.getElementById("t-msg");
  try {
    const env = await api(`/api/sim/positions/${id}/close`, { method: "POST" });
    const p = env.data.position;
    announce(msg, `Simulated position #${id} closed at ${fmtPrice(p.close_price, 3)}: net ${fmtMoney(p.realized_net, { sign: true })}.`, "ok");
  } catch (err) { announce(msg, `Close refused: ${err.message}`, "error"); }
  await refreshAll();
}

async function protect(id, sl, tp) {
  const msg = document.getElementById("t-msg");
  try {
    await api(`/api/sim/positions/${id}`, { method: "PATCH", body: { stop_loss: sl.trim() || null, take_profit: tp.trim() || null } });
    announce(msg, `Stop loss / take profit updated for simulated position #${id}.`, "ok");
  } catch (err) { announce(msg, `Update refused: ${err.message}`, "error"); }
  await refreshPositions();
}

// ------------------------------------------------------------------ equity + ledger

async function refreshEquity() {
  let env;
  try { env = await api("/api/sim/equity?limit=2000"); } catch (_) { return; }
  const seen = new Map();
  for (const row of env.data) seen.set(Math.floor(Date.parse(row.ts) / 1000), num(row.equity));
  const points = [...seen.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
  if (!state.equityChart) state.equityChart = createLineChart(document.getElementById("equity-chart"));
  state.equityChart.series.setData(points);
  state.equityChart.chart.timeScale().fitContent();
  setText("eq-note", `${points.length} snapshot(s) · recorded on every simulated fill and every minute while positions are open`);
}

async function refreshLedger() {
  let env;
  try { env = await api("/api/sim/ledger?limit=50"); } catch (_) { return; }
  const body = clear(document.getElementById("ledger-body"));
  for (const r of env.data) {
    body.append(el("tr", {}, [el("td", { text: fmtTime(r.ts, true) }), el("td", { text: r.kind.replace("_", " ") }),
      el("td", { class: `num ${pnlClass(r.amount)}`, text: fmtMoney(r.amount, { sign: true }) }),
      el("td", { class: "num", text: fmtMoney(r.balance_after) }), el("td", { text: r.note })]));
  }
}

// ------------------------------------------------------------------ new account

function initNewAccount() {
  const form = document.getElementById("new-form");
  const msg = document.getElementById("new-msg");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!window.confirm("Start a new virtual account? The current one is kept (inactive) with its history.")) return;
    const f = new FormData(form);
    const body = {
      account: { name: f.get("name"), currency: "USD", starting_balance: f.get("starting_balance"),
                 leverage: Number(f.get("leverage")), max_quote_age_seconds: Number(f.get("max_quote_age_seconds")),
                 allow_delayed_fills: f.get("allow_delayed_fills") === "on" },
      costs: { spread_mode: f.get("spread_mode"), configured_spread: f.get("configured_spread") || null,
               commission_per_lot_per_side: f.get("commission_per_lot_per_side"), slippage: f.get("slippage") },
    };
    try {
      await api("/api/sim/accounts", { method: "POST", body });
      announce(msg, "New virtual account created.", "ok");
      document.getElementById("presets").replaceChildren();
      await refreshAll();
    } catch (err) { announce(msg, `Not created: ${err.message}`, "error"); }
  });
}

// ------------------------------------------------------------------ wiring

async function refreshAll() {
  await refreshAccount();
  await Promise.all([refreshPositions(), refreshEquity(), refreshLedger()]);
  scheduleEstimate();
}

function onQuote(envelope) {
  const snap = envelope && envelope.data;
  if (!snap) return;
  document.getElementById("top-state").replaceChildren(stateBadge(snap.state));
  document.getElementById("t-state").replaceChildren(stateBadge(snap.state));
  const q = snap.quote;
  setText("top-price", q ? fmtPrice(q.mid ?? q.last, 2) : null);
  const details = snap.provider_details;
  state.brokerSpec = details && details.symbol_spec ? details.symbol_spec : null;
}

/** ?signal=<id>: prefill side and stop loss from a STORED rule-based signal (the user still decides). */
async function prefillFromSignal(setSide) {
  const id = new URLSearchParams(window.location.search).get("signal");
  if (!id || !/^sig_[0-9a-f]{20}$/.test(id)) return;
  const box = document.getElementById("sig-prefill");
  try {
    const s = await api(`/api/signals/${encodeURIComponent(id)}`);
    if (s.mode !== "live") throw new Error("replay signals are historical observations and cannot prefill a ticket");
    if (s.direction !== "BULLISH" && s.direction !== "BEARISH") throw new Error(`signal is ${s.direction}`);
    setSide(s.direction === "BULLISH" ? "buy" : "sell");
    const inv = (s.invalidation || [])[0];
    if (inv) document.getElementById("sl").value = String(inv.price);
    state.signalId = s.id;
    clear(box);
    box.append(el("strong", { text: `Prefilled from a ${s.direction} rule-based signal` }),
      ` (score ${s.score > 0 ? "+" : ""}${s.score}, ${s.score_label}; data cut-off ${fmtTime(s.cutoff, true)}). `,
      inv ? `Stop loss set at the invalidation level: ${inv.condition}. ` : "No invalidation level available. ",
      "Review and change anything before placing a SIMULATED order. A signal is an observation, not advice.");
    box.hidden = false;
  } catch (err) {
    announce(box, `Could not load the signal: ${err.message}`, "warn");
  }
}

async function main() {
  document.getElementById("sum-kind").replaceWith(kindBadge("simulated"));
  try { setDisplayZone((await api("/api/ui-config")).display_timezone || "UTC"); } catch (_) { /* UTC */ }
  const buy = document.getElementById("side-buy"), sell = document.getElementById("side-sell");
  const setSide = (side) => { state.side = side; buy.setAttribute("aria-pressed", String(side === "buy"));
                              sell.setAttribute("aria-pressed", String(side === "sell")); scheduleEstimate(); };
  buy.addEventListener("click", () => setSide("buy"));
  prefillFromSignal(setSide);
  sell.addEventListener("click", () => setSide("sell"));
  document.getElementById("lots").addEventListener("input", () => { updateOunces(); scheduleEstimate(); });
  document.getElementById("submit").addEventListener("click", submit);
  initNewAccount();
  subscribeQuotes(onQuote, () => {});
  refreshAll();
  setInterval(() => { if (!document.hidden) { refreshAccount(); refreshPositions(); scheduleEstimate(); } }, 3000);
  setInterval(() => { if (!document.hidden) { refreshEquity(); refreshLedger(); } }, 30000);
}

main();
