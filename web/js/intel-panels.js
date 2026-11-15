// Phase 2 panels shared by the dashboard and the replay page. Built with el()/textContent only.
// Wording rule: observations and candidates, never promises. The score is NOT a probability.
import { clear, el, fmtAge, fmtPrice, fmtTime } from "./ui.js";

const signed = (n) => (n > 0 ? `+${n}` : String(n));
const dash = (v) => (v === null || v === undefined || v === "" ? "—" : v);

function list(items, render, empty) {
  if (!items || !items.length) return el("p", { class: "muted small", text: empty });
  return el("ul", { class: "evidence" }, items.map(render));
}

function table(headers, rows, { cls = "", empty = "none" } = {}) {
  if (!rows.length) return el("p", { class: "muted small", text: empty });
  return el("div", { class: "table-wrap" }, el("table", { class: `compact ${cls}`.trim() }, [
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, rows),
  ]));
}

function evidenceItem(e) {
  return el("li", {}, [
    el("span", { class: `pill ${e.score > 0 ? "bull" : e.score < 0 ? "bear" : ""}`, text: signed(e.score) }),
    el("span", { class: "rule", text: e.rule }),
    el("span", { class: "obs small", text: e.observation }),
  ]);
}

const trendClass = (state) => `trend-${state === "bullish" ? "up" : state === "bearish" ? "down" : "flat"}`;

function statusTag(status) {
  const map = { confirmed: "tag-ok", developing: "tag-dev", untouched: "tag-ok", testing: "tag-dev", swept: "tag-gold",
                broken: "tag-muted", unmitigated: "tag-ok", partially_mitigated: "tag-dev", fully_mitigated: "tag-muted",
                touched: "tag-dev", invalidated: "tag-muted" };
  return el("span", { class: `tag ${map[status] || ""}`, text: String(status).replace("_", " ") });
}

export function signalBadge(direction) {
  const cls = { BULLISH: "bull", BEARISH: "bear", NEUTRAL: "neutral", "NO SIGNAL": "none" }[direction] || "none";
  return el("span", { class: `sig-badge ${cls}`, text: direction });
}

// ------------------------------------------------------------------ F. signal centre
/** Signal Centre. ``prefill``: show the paper-ticket link for a stored directional signal. */
export function renderSignal(node, s, { prefill = true } = {}) {
  clear(node);
  if (!s) { node.append(el("p", { class: "muted", text: "No signal computed yet." })); return; }
  node.append(el("div", { class: "sig-head" }, [
    signalBadge(s.direction),
    el("div", { class: "sig-score" }, [
      el("span", { class: "num big", text: signed(s.score) }),
      el("span", { class: "muted small", text: ` ${s.score_label} · threshold ±${s.threshold}` }),
    ]),
  ]));
  const fresh = Object.entries(s.freshness || {});
  const stale = fresh.filter(([, f]) => !f.fresh).map(([tf]) => tf);
  node.append(el("dl", { class: "kv small sig-meta" }, [
    el("dt", { text: "Signal time (data cut-off)" }), el("dd", { class: "num", text: fmtTime(s.cutoff, true) }),
    el("dt", { text: "Computed" }), el("dd", { class: "num", text: fmtTime(s.generated_at, true) }),
    el("dt", { text: "Data freshness" }),
    el("dd", { class: stale.length ? "warn-text" : "", text: fresh.length
      ? (stale.length ? `stale: ${stale.join(", ")}` : `all fresh (${fresh.map(([tf, f]) => `${tf} ${fmtAge(f.age_seconds)}`).join(" · ")})`)
      : "—" }),
    el("dt", { text: "Source" }), el("dd", { text: `${s.source.series} · ${s.source.provider || "?"} · ${s.source.market_state || s.mode}` }),
  ]));
  node.append(el("p", { class: "sig-expl small", text: s.explanation }));
  if (s.gates_failed && s.gates_failed.length) {
    node.append(el("h3", { text: "Why there is no signal" }));
    node.append(list(s.gates_failed, (g) => el("li", { class: "small", text: g }), ""));
  }
  node.append(el("h3", { text: s.direction === "NEUTRAL" ? `Evidence leaning ${s.lean}` : "Supporting evidence" }));
  node.append(list(s.supporting, evidenceItem, "none"));
  node.append(el("h3", { text: s.direction === "NEUTRAL" ? "Opposing evidence" : "Conflicting evidence" }));
  node.append(list(s.conflicting, evidenceItem, "none"));
  if (s.neutral && s.neutral.length) {
    node.append(el("h3", { text: "Neutral (no score)" }));
    node.append(list(s.neutral, evidenceItem, ""));
  }
  node.append(el("h3", { text: "Invalidation" }));
  node.append(list(s.invalidation, (i) => el("li", { class: "small" }, [el("strong", { text: i.condition }), ` — ${i.meaning}`]),
    s.direction === "BULLISH" || s.direction === "BEARISH" ? "no invalidation level available" : "not applicable (no directional signal)"));
  const ctx = s.context || {};
  const vol = Object.entries(ctx.volatility || {}).map(([tf, v]) => `${tf} ${v.condition || "n/a"}`).join(" · ");
  node.append(el("h3", { text: "Context" }));
  node.append(el("dl", { class: "kv small" }, [
    el("dt", { text: "Spread" }), el("dd", { text: ctx.spread !== null && ctx.spread !== undefined
      ? `${fmtPrice(ctx.spread)} (${ctx.spread_source}${ctx.spread_vs_atr_m5 !== null ? `, ${ctx.spread_vs_atr_m5}× ATR M5` : ""})` : ctx.spread_source }),
    el("dt", { text: "Volatility" }), el("dd", { text: vol || "—" }),
    el("dt", { text: "Session" }), el("dd", { text: ctx.session ? ctx.session.current : "—" }),
    el("dt", { text: "Rules" }), el("dd", { text: `${s.definition} · config ${String(s.config_fingerprint).slice(0, 10)}` }),
  ]));
  if (prefill && s.stored_id && (s.direction === "BULLISH" || s.direction === "BEARISH")) {
    node.append(el("a", { class: "btn", href: `/paper?signal=${encodeURIComponent(s.stored_id)}`,
                          text: "Prefill a SIMULATED paper ticket from this signal" }));
  }
  node.append(el("p", { class: "muted small", text: s.disclaimer }));
}

// ------------------------------------------------------------------ D. multi-timeframe (side by side)
const MTF_ORDER = ["M1", "M5", "M15", "H1"];

export function renderMTF(node, mtf) {
  clear(node);
  if (!mtf) return;
  const by = Object.fromEntries(mtf.timeframes.map((r) => [r.timeframe, r]));
  const cards = MTF_ORDER.filter((tf) => by[tf]).map((tf) => {
    const r = by[tf];
    const e = r.last_event;
    const above = r.nearest_liquidity_above;
    const below = r.nearest_liquidity_below;
    return el("article", { class: "mtf-card" }, [
      el("header", {}, [el("span", { class: "tf", text: tf }), el("span", { class: `state ${trendClass(r.state)}`, text: r.state })]),
      el("dl", { class: "kv small" }, [
        el("dt", { text: "Last break" }), el("dd", { text: e ? `${e.type} ${e.direction} ${fmtPrice(e.price)}` : "—" }),
        el("dt", { text: "Swings H/L" }), el("dd", { text: `${dash(r.last_high_label)} / ${dash(r.last_low_label)}` }),
        el("dt", { text: "Unbroken swing high" }), el("dd", { class: "num", text: r.reference_high !== null ? fmtPrice(r.reference_high) : "—" }),
        el("dt", { text: "Unbroken swing low" }), el("dd", { class: "num", text: r.reference_low !== null ? fmtPrice(r.reference_low) : "—" }),
        el("dt", { text: "Volatility" }), el("dd", { text: `${dash(r.volatility.condition)}${r.volatility.atr ? ` · ATR ${fmtPrice(r.volatility.atr)}` : ""}` }),
        el("dt", { text: "Liquidity above" }), el("dd", { text: above ? `${above.label} ${fmtPrice(above.price)}` : "—" }),
        el("dt", { text: "Liquidity below" }), el("dd", { text: below ? `${below.label} ${fmtPrice(below.price)}` : "—" }),
        el("dt", { text: "Open zones" }), el("dd", { text: `${r.open_fvg} FVG · ${r.open_order_blocks} OB` }),
        el("dt", { text: "Data" }), el("dd", { class: r.freshness.fresh ? "" : "warn-text", text: r.freshness.fresh ? `fresh · ${r.bars} closed` : "stale" }),
      ]),
      el("p", { class: "muted small", text: (r.state_explanation || []).join("; ") }),
    ]);
  });
  node.append(el("div", { class: "mtf-grid" }, cards));
  node.append(el("p", { class: "small" }, [el("strong", { text: `Alignment: ${mtf.alignment.label}. ` }), mtf.alignment.note]));
  node.append(list(mtf.disagreements, (d) => el("li", { class: "small", text: d }), "No disagreements between timeframes."));
}

// ------------------------------------------------------------------ A. market structure
export function renderStructure(node, ov) {
  clear(node);
  if (!ov) return;
  const st = ov.structure;
  node.append(el("div", { class: "sig-head" }, [
    el("span", { class: `state-big ${trendClass(st.state)}`, text: st.state }),
    el("span", { class: "muted small", text: `${ov.timeframe} · ${st.definition} · swing strength ${ov.display ? ov.display.swing_strength : st.params.swing_strength}`
      + (ov.display && ov.display.swing_strength !== ov.display.configured_swing_strength ? " (display override)" : "") }),
  ]));
  node.append(el("p", { class: "small muted", text: (st.state_explanation || []).join("; ") }));
  node.append(el("dl", { class: "kv small" }, [
    el("dt", { text: "Unbroken swing high" }), el("dd", { class: "num", text: st.reference_high ? `${fmtPrice(st.reference_high.price)} (${st.reference_high.label})` : "—" }),
    el("dt", { text: "Unbroken swing low" }), el("dd", { class: "num", text: st.reference_low ? `${fmtPrice(st.reference_low.price)} (${st.reference_low.label})` : "—" }),
  ]));
  node.append(el("h3", { text: "Recent breaks (confirmed)" }));
  node.append(table(["Break", "Level", "Close", "Candle", "Known at"], (st.events || []).slice(-6).reverse().map((e) => el("tr", {}, [
    el("td", { class: e.direction === "bullish" ? "up" : "down", text: `${e.type} ${e.direction}` }),
    el("td", { class: "num", text: fmtPrice(e.price) }), el("td", { class: "num", text: fmtPrice(e.close) }),
    el("td", { class: "num", text: fmtTime(e.time, true) }), el("td", { class: "num", text: fmtTime(e.known_at) }),
  ])), { empty: "No confirmed break in the loaded candles." }));
  node.append(el("h3", { text: "Recent swings (confirmed)" }));
  node.append(table(["Swing", "Price", "Candle", "Confirmed at"], (st.swings || []).slice(-8).reverse().map((s) => el("tr", {}, [
    el("td", { text: `${s.label} (${s.type === "swing_high" ? "high" : "low"})` }), el("td", { class: "num", text: fmtPrice(s.price) }),
    el("td", { class: "num", text: fmtTime(s.time, true) }), el("td", { class: "num", text: fmtTime(s.known_at) }),
  ])), { empty: "No confirmed swing yet." }));
  node.append(el("h3", { text: "Developing (not confirmed)" }));
  node.append(list(st.developing, (d) => el("li", { class: "small" }, [statusTag("developing"), " ", d.explanation]),
    "Nothing developing: no unconfirmed swing or forming break."));
}

// ------------------------------------------------------------------ B. liquidity map
const LEVEL_ORDER = { PWH: 0, PDH: 1, EQH: 2, session_high: 3, session_low: 4, EQL: 5, PDL: 6, PWL: 7 };

export function renderLiquidity(node, liq, lastClose = null) {
  clear(node);
  if (!liq) return;
  const levels = [...(liq.levels || [])].sort((a, b) => b.price - a.price || (LEVEL_ORDER[a.type] ?? 9) - (LEVEL_ORDER[b.type] ?? 9));
  const rows = levels.map((l) => el("tr", { class: lastClose !== null && Math.abs(l.price - lastClose) < 1e-9 ? "here" : "" }, [
    el("td", { text: l.label + (l.touches ? ` ×${l.touches}` : "") }),
    el("td", { class: "num", text: fmtPrice(l.price) }),
    el("td", { class: "num", text: lastClose !== null ? `${l.price >= lastClose ? "+" : ""}${fmtPrice(l.price - lastClose)}` : "—" }),
    el("td", {}, statusTag(l.status)),
    el("td", { class: "num", text: fmtTime(l.known_at) }),
  ]));
  node.append(table(["Level", "Price", "Distance", "Status", "Known at"], rows, { empty: "No liquidity levels in the loaded candles." }));
  if (liq.developing && liq.developing.length) {
    node.append(el("p", { class: "muted small" }, [statusTag("developing"), ` ${liq.developing.map((d) => `${d.label} ${fmtPrice(d.price)}`).join(" · ")}`]));
  }
  node.append(el("h3", { text: "Sweep and break observations" }));
  node.append(list((liq.events || []).slice(-6).reverse(), (e) => el("li", { class: "small" }, [
    el("span", { class: `pill ${e.type === "sweep" ? "gold" : ""}`, text: e.status === "developing" ? "developing" : e.type }),
    el("span", { class: "rule", text: e.label }),
    el("span", { class: "obs", text: e.explanation }),
  ]), "No interactions with tracked levels yet."));
  node.append(el("p", { class: "muted small", text: `${liq.definition}: a potential sweep needs a close back inside the level (same candle or within ${liq.params ? liq.params.sweep_return_bars : 3} candles); a close beyond it with no return is a break, not a sweep.` }));
}

// ------------------------------------------------------------------ C. price action (FVG / order blocks)
export function renderPriceAction(node, ov) {
  clear(node);
  if (!ov) return;
  const g = ov.fvg;
  const ob = ov.order_blocks;
  const zone = (z) => `${fmtPrice(z.zone.bottom)} – ${fmtPrice(z.zone.top)}`;
  node.append(el("h3", { text: `Fair value gaps (${ov.timeframe})` }));
  const fvgRows = [...(g.open || []).slice(-8).reverse(), ...(g.recent_filled || []).slice(-3).reverse()].map((x) => el("tr", {}, [
    el("td", { class: x.direction === "bullish" ? "up" : "down", text: x.direction }),
    el("td", { class: "num", text: zone(x) }), el("td", { class: "num", text: fmtPrice(x.size) }),
    el("td", {}, [statusTag(x.mitigation), x.mitigation === "partially_mitigated" ? ` ${x.mitigated_pct}%` : ""]),
    el("td", { class: "num", text: fmtTime(x.known_at, true) }),
  ]));
  node.append(table(["Direction", "Zone", "Size", "Mitigation", "Formed (known)"], fvgRows, { empty: "No fair value gaps in the loaded candles." }));
  if (g.developing && g.developing.length) node.append(el("p", { class: "muted small" }, [statusTag("developing"), " a gap is forming on the current candle"]));
  node.append(el("p", { class: "muted small", text: `${g.definition}: ${g.filtered_small} gap(s) below the minimum size ignored, ${g.merged} overlapping gap(s) merged.` }));
  node.append(el("h3", { text: "Order-block candidates (algorithmic)" }));
  const obRows = [...(ob.open || []).slice(-6).reverse(), ...(ob.recent_invalidated || []).slice(-3).reverse()].map((x) => el("tr", {}, [
    el("td", { class: x.direction === "bullish" ? "up" : "down", text: x.direction }),
    el("td", { class: "num", text: zone(x) }),
    el("td", {}, statusTag(x.mitigation)),
    el("td", { class: "small", text: `${x.structure_type} · ${x.displacement_atr} ATR` }),
    el("td", { class: "num", text: fmtTime(x.known_at, true) }),
  ]));
  node.append(table(["Direction", "Zone", "Status", "From break", "Known at"], obRows, { empty: "No order-block candidates in the loaded candles." }));
  node.append(el("p", { class: "muted small", text: `${ob.definition}: the last opposite-colour candle before a displacement that broke structure. A candidate, not an institutional order.` }));
}

// ------------------------------------------------------------------ E. session intelligence
export function renderSessions(node, ss) {
  clear(node);
  if (!ss) return;
  const ldnNy = (ss.overlaps || []).find((o) => o.sessions.includes("London") && o.sessions.includes("New York"));
  node.append(el("dl", { class: "kv small" }, [
    el("dt", { text: "Current session" }), el("dd", {}, [el("strong", { text: ss.current })]),
    el("dt", { text: "London/New York overlap" }),
    el("dd", { text: ldnNy ? `${ldnNy.status} (${fmtTime(ldnNy.start)} – ${fmtTime(ldnNy.end)})` : "none today" }),
    el("dt", { text: "Next session" }), el("dd", { text: ss.next ? `${ss.next.name} at ${fmtTime(ss.next.start, true)}` : "—" }),
  ]));
  for (const s of ss.sessions) {
    const range = s.range !== undefined ? `H ${fmtPrice(s.high)} · L ${fmtPrice(s.low)} · range ${fmtPrice(s.range)}` : "no candles in window";
    const vol = [s.range_vs_atr !== undefined ? `${s.range_vs_atr}× ATR(M5)` : null, s.compare && s.compare.note ? s.compare.note : null]
      .filter(Boolean).join(" · ");
    node.append(el("div", { class: `session${s.status === "active" ? " active" : ""}` }, [
      el("span", { class: "name", text: s.name }),
      el("span", { class: "tag", text: `${s.local_hours} ${s.zone.split("/").pop().replace("_", " ")}` }),
      el("span", { class: "tag", text: s.status }),
      el("span", { class: "num small", text: range }),
      el("span", { class: "muted small", text: vol ? `Volatility: ${vol}` : "" }),
    ]));
  }
  const br = (ss.breakouts || []).slice(-4).reverse();
  if (br.length) {
    node.append(el("h3", { text: "Range breakouts (observations)" }));
    node.append(list(br, (b) => el("li", { class: "small", text: b.explanation }), ""));
  }
  node.append(el("p", { class: "muted small", text: ss.timezone_note }));
}

export function renderVolatility(node, v) {
  clear(node);
  if (!v) return;
  const rows = Object.entries(v.timeframes).map(([tf, p]) => el("tr", {}, [
    el("th", { scope: "row", text: tf }),
    el("td", { class: "num", text: p.atr !== null && p.atr !== undefined ? fmtPrice(p.atr) : "—" }),
    el("td", { class: "num", text: p.atr_pct !== null && p.atr_pct !== undefined ? `${p.atr_pct}%` : "—" }),
    el("td", { class: "small", text: p.condition }),
    el("td", { class: "num", text: p.last_range_percentile !== null && p.last_range_percentile !== undefined ? `${p.last_range_percentile}` : "—" }),
    el("td", { class: "num", text: p.body_to_range !== null && p.body_to_range !== undefined ? p.body_to_range.toFixed(2) : "—" }),
  ]));
  node.append(table(["TF", "ATR", "ATR %", "Condition", "Range pctl", "Body/range"], rows));
  if (v.adr) {
    node.append(el("p", { class: "small", text: `ADR (${v.adr.days_used} complete days): ${fmtPrice(v.adr.adr)} (${v.adr.adr_pct}% of price) · today ${fmtPrice(v.adr.today_range)} = ${v.adr.today_pct_of_adr ?? "—"}% of ADR` }));
  }
  node.append(el("p", { class: "muted small", text: `${v.definition}: ATR(${v.params.atr_period}) Wilder; expansion = ATR(${v.params.fast_atr})/ATR(${v.params.slow_atr}) ≥ ${v.params.expansion_ratio}, contraction ≤ ${v.params.contraction_ratio}; closed candles only.` }));
}

// ------------------------------------------------------------------ H. performance summary card
export function renderPerformanceSummary(node, run) {
  clear(node);
  if (!run) { node.append(el("p", { class: "muted small", text: "No completed evaluation yet. Run one on the Performance page." })); return; }
  const r = run.result;
  node.append(el("p", { class: "small", text: `${run.series_key} · ${fmtTime(r.range.start, true)} → ${fmtTime(r.range.end, true)} · ${r.steps} steps` }));
  for (const s of r.streams) {
    const a = s.analytical;
    const st = s.strategy;
    node.append(el("dl", { class: "kv small" }, [
      el("dt", { text: `${s.timeframe} signals` }), el("dd", { text: `${s.counts.bullish} bullish · ${s.counts.bearish} bearish · ${s.counts.neutral_observations} neutral` }),
      el("dt", { text: "Analytical" }), el("dd", { text: `${a.favourable} favourable · ${a.adverse} adverse · ${a.flat} flat` }),
      el("dt", { text: "Hypothetical net" }), el("dd", { class: st.net_pnl > 0 ? "up" : st.net_pnl < 0 ? "down" : "", text: `${st.trades} trades · ${fmtPrice(st.net_pnl)} USD · max DD ${fmtPrice(st.max_drawdown)}` }),
    ]));
  }
  node.append(el("p", { class: "muted small", text: r.disclaimer }));
}
