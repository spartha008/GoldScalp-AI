// Lightweight Charts wrapper (vendored v5.2.1, global LightweightCharts).
// Overlays never move the price scale: markers, price lines and a zone primitive whose
// autoscaleInfo() is null. Bars are UTC; axis labels and the crosshair use the display timezone.
import { selectMarkers } from "./annotation-select.js";
import { displayZone } from "./ui.js";

const LWC = window.LightweightCharts;
let live = 0;

function zoned(time, options) {
  return new Intl.DateTimeFormat("en-GB", { timeZone: displayZone(), hour12: false, ...options })
    .format(new Date(time * 1000));
}

function tickMark(time, type) {
  if (type === 0) return zoned(time, { year: "numeric" });
  if (type === 1) return zoned(time, { month: "short" });
  if (type === 2) return zoned(time, { day: "2-digit", month: "short" });
  return zoned(time, { hour: "2-digit", minute: "2-digit" });
}

function crosshairTime(time) {
  return zoned(time, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZoneName: "short" });
}

export const COLORS = {
  up: "#2bb594", down: "#e5484d", gold: "#d4af37", goldSoft: "rgba(212,175,55,0.55)",
  text: "#b9b4a6", grid: "#141417", border: "#26262a", forming: "#e6c868",
  swing: "rgba(185,180,166,0.75)", liqBuy: "rgba(96,165,250,0.85)", liqSell: "rgba(244,114,182,0.85)",
  developing: "rgba(230,200,104,0.55)",
};
const HIGH_LEVELS = ["EQH", "PDH", "PWH", "session_high"];

const ZONE_STYLE = {
  fvg: { bullish: ["rgba(43,181,148,0.10)", "rgba(43,181,148,0.45)"], bearish: ["rgba(229,72,77,0.10)", "rgba(229,72,77,0.45)"] },
  ob: { bullish: ["rgba(212,175,55,0.10)", "rgba(212,175,55,0.55)"], bearish: ["rgba(167,139,250,0.10)", "rgba(167,139,250,0.55)"] },
};

const epoch = (iso) => Math.floor(Date.parse(iso) / 1000);

// ------------------------------------------------------------------ zone primitive (FVG / order blocks)
class ZoneRenderer {
  constructor(source) { this.source = source; }
  draw(target) {
    const { chart, series, zones, firstTime } = this.source;
    if (!chart || !series || !zones.length) return;
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context;
      const hr = scope.horizontalPixelRatio;
      const vr = scope.verticalPixelRatio;
      const ts = chart.timeScale();
      ctx.save();
      ctx.font = `${Math.round(10 * vr)}px Cascadia Mono, Consolas, monospace`;
      for (const z of zones) {
        let x1 = ts.timeToCoordinate(z.from);
        if (x1 === null) {
          if (firstTime !== null && z.from < firstTime) x1 = 0; else continue;
        }
        let x2 = z.to ? ts.timeToCoordinate(z.to) : null;
        if (x2 === null) x2 = scope.mediaSize.width;
        const y1 = series.priceToCoordinate(z.top);
        const y2 = series.priceToCoordinate(z.bottom);
        if (y1 === null || y2 === null) continue;
        const [fill, stroke] = z.developing ? ["rgba(230,200,104,0.05)", COLORS.developing]
          : (ZONE_STYLE[z.kind] || ZONE_STYLE.fvg)[z.direction] || ZONE_STYLE.fvg.bullish;
        const left = Math.round(Math.min(x1, x2) * hr);
        const top = Math.round(Math.min(y1, y2) * vr);
        const width = Math.max(1, Math.round(Math.abs(x2 - x1) * hr));
        const height = Math.max(1, Math.round(Math.abs(y2 - y1) * vr));
        ctx.fillStyle = fill;
        ctx.fillRect(left, top, width, height);
        ctx.strokeStyle = stroke;
        ctx.lineWidth = Math.max(1, Math.round(hr));
        if (z.developing) ctx.setLineDash([2 * hr, 3 * hr]);
        else if (z.kind === "ob") ctx.setLineDash([4 * hr, 3 * hr]);
        else ctx.setLineDash([]);
        ctx.strokeRect(left + 0.5, top + 0.5, width - 1, height - 1);
        if (height > 9 * vr) {
          ctx.fillStyle = stroke;
          ctx.fillText(z.label, left + 4 * hr, top + 10 * vr);
        }
      }
      ctx.restore();
    });
  }
}

class ZonePaneView {
  constructor(source) { this.source = source; }
  zOrder() { return "bottom"; }
  renderer() { return new ZoneRenderer(this.source); }
}

class ZonesPrimitive {
  constructor() { this.zones = []; this.firstTime = null; this.views = [new ZonePaneView(this)]; }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.requestUpdate = requestUpdate; }
  detached() { this.chart = null; this.series = null; }
  paneViews() { return this.views; }
  updateAllViews() {}
  autoscaleInfo() { return null; }   // zones never change the price scale
  set(zones, firstTime) { this.zones = zones; this.firstTime = firstTime; if (this.requestUpdate) this.requestUpdate(); }
}

export function liveCharts() { return live; }

export function createCandleChart(container, { precision = 2 } = {}) {
  if (!LWC) throw new Error("chart library failed to load");
  const chart = LWC.createChart(container, {
    autoSize: true,
    layout: { background: { type: "solid", color: "#050505" }, textColor: COLORS.text,
              fontFamily: "Cascadia Mono, Consolas, ui-monospace, monospace", fontSize: 11 },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    rightPriceScale: { borderColor: COLORS.border },
    timeScale: { borderColor: COLORS.border, timeVisible: true, secondsVisible: false, rightOffset: 4,
                 tickMarkFormatter: tickMark },
    localization: { timeFormatter: crosshairTime },
    crosshair: { mode: LWC.CrosshairMode.Normal,
                 vertLine: { color: COLORS.goldSoft, labelBackgroundColor: "#9c7c1c" },
                 horzLine: { color: COLORS.goldSoft, labelBackgroundColor: "#9c7c1c" } },
  });
  live += 1;
  const series = chart.addSeries(LWC.CandlestickSeries, {
    upColor: COLORS.up, downColor: COLORS.down, borderVisible: false,
    wickUpColor: COLORS.up, wickDownColor: COLORS.down,
    priceFormat: { type: "price", precision, minMove: 1 / 10 ** precision },
  });
  const markers = LWC.createSeriesMarkers(series, []);
  const zones = new ZonesPrimitive();
  series.attachPrimitive(zones);
  let lines = [];
  let disposed = false;
  let times = new Set();
  let firstTime = null;
  let lastTime = null;
  const view = { ov: null, show: {}, opts: {} };
  let pending = false;

  function clearLines() {
    for (const line of lines) series.removePriceLine(line);
    lines = [];
  }

  /** Markers depend on the visible range (zoom-aware): only annotations inside it count toward the
   *  maximum, text is hidden when zoomed out, swing markers are dropped when very zoomed out. */
  function renderMarkers() {
    pending = false;
    const { ov, show, opts } = view;
    if (!ov) { markers.setMarkers([]); return; }
    const range = chart.timeScale().getVisibleRange();
    const logical = chart.timeScale().getVisibleLogicalRange();
    const bars = logical ? Math.max(0, Math.round(logical.to - logical.from)) : 0;
    const groups = [];   // priority order: confirmed breaks, developing, sweeps, swings
    if (show.structure) {
      groups.push({ kind: "events", markers: (ov.structure.events || []).map((e) => {
        const bull = e.direction === "bullish";
        return { time: epoch(e.time), position: bull ? "belowBar" : "aboveBar", shape: bull ? "arrowUp" : "arrowDown",
                 color: e.type === "CHoCH" ? COLORS.gold : (bull ? COLORS.up : COLORS.down), text: e.type };
      }) });
    }
    if (show.developing) {
      const dev = [];
      for (const d of ov.structure.developing || []) {
        const swing = d.type.startsWith("candidate_");
        dev.push({ time: epoch(d.time), position: d.direction === "bearish" ? "aboveBar" : "belowBar",
                   shape: "circle", size: swing ? 0.4 : 0.8, color: COLORS.developing,
                   text: swing ? "swing?" : (d.direction === "bullish" ? "break? ↑" : "break? ↓") });
      }
      for (const e of ov.liquidity.events || []) {
        if (e.status !== "developing") continue;
        dev.push({ time: epoch(e.time), position: HIGH_LEVELS.includes(e.level_type) ? "aboveBar" : "belowBar",
                   shape: "square", size: 0.6, color: COLORS.developing, text: "break?" });
      }
      groups.push({ kind: "developing", markers: dev });
    }
    if (show.sweeps) {
      groups.push({ kind: "sweeps", markers: (ov.liquidity.events || []).filter((e) => e.status === "confirmed").map((e) => ({
        time: epoch(e.time), position: HIGH_LEVELS.includes(e.level_type) ? "aboveBar" : "belowBar", shape: "square",
        size: 0.6, color: e.type === "sweep" ? COLORS.gold : COLORS.text, text: e.type === "sweep" ? "sweep?" : "break",
      })) });
    }
    if (show.swings) {
      groups.push({ kind: "swings", markers: (ov.structure.swings || []).map((s) => ({
        time: epoch(s.time), position: s.type === "swing_high" ? "aboveBar" : "belowBar", shape: "circle", size: 0.4,
        color: COLORS.swing, text: s.label,
      })) });
    }
    const max = opts.max || 10;
    const { markers: chosen, labels, swingsAllowed } = selectMarkers(groups, { range, bars, max, lastTime, times });
    markers.setMarkers(chosen);
    if (opts.onDensity) {
      opts.onDensity(!labels ? `Zoomed out (${bars} candles): labels hidden${swingsAllowed ? "" : ", swings hidden"}. `
        + "Zoom in to read them. Arrows = BOS/CHoCH, circles = swings or developing items, squares = sweeps/breaks."
        : `${chosen.length} of max ${max} annotations in view.`);
    }
  }

  function scheduleMarkers() {
    if (pending || !view.ov) return;
    pending = true;
    setTimeout(renderMarkers, 40);   // debounce; also runs when the page is not painting
  }
  chart.timeScale().subscribeVisibleLogicalRangeChange(scheduleMarkers);

  return {
    chart, series,
    setCandles(candles, { fit = false } = {}) {
      const data = candles.map((c) => {
        const point = { time: c.time, open: c.open, high: c.high, low: c.low, close: c.close };
        if (!c.closed) { point.color = "rgba(230,200,104,0.25)"; point.borderColor = COLORS.forming; point.wickColor = COLORS.forming; }
        else if (c.partial) { point.color = "rgba(142,163,184,0.35)"; point.wickColor = "rgba(142,163,184,0.6)"; }   // period only partly covered
        return point;
      });
      series.setData(data);
      times = new Set(data.map((d) => d.time));
      firstTime = data.length ? data[0].time : null;
      lastTime = data.length ? data[data.length - 1].time : null;
      if (fit) chart.timeScale().fitContent();
      scheduleMarkers();
    },
    /** Phase 2 overlays from /api/analysis/intelligence (one timeframe).
     *  show: {structure, swings, liquidity, sweeps, fvg, ob, developing}; opts: {max, onDensity}. */
    setIntelligence(ov, show = {}, opts = {}) {
      view.ov = ov; view.show = show; view.opts = opts;
      clearLines();
      const max = opts.max || 10;
      const last = ov ? (ov.last_close ?? null) : null;
      const nearest = (items, price) => (last === null ? items.slice(-max)
        : [...items].sort((a, b) => Math.abs(price(a) - last) - Math.abs(price(b) - last)).slice(0, max));
      if (ov && show.structure) {
        for (const ref of [ov.structure.reference_high, ov.structure.reference_low]) {
          if (ref) lines.push(series.createPriceLine({ price: ref.price, color: COLORS.goldSoft, lineWidth: 1, lineStyle: 2,
                                                       axisLabelVisible: false, title: `${ref.label} (unbroken)` }));
        }
      }
      if (ov && show.liquidity) {
        const active = (ov.liquidity.levels || []).filter((l) => l.status === "untouched" || l.status === "testing");
        for (const l of nearest(active, (x) => x.price).slice(0, Math.min(8, max))) {
          lines.push(series.createPriceLine({ price: l.price, lineWidth: 1, lineStyle: l.status === "testing" ? 1 : 3,
                                              color: l.side === "buy-side" ? COLORS.liqBuy : COLORS.liqSell,
                                              axisLabelVisible: true, title: shortLevel(l) + (l.status === "testing" ? " (testing)" : "") }));
        }
        if (show.developing) {
          for (const d of ov.liquidity.developing || []) {
            lines.push(series.createPriceLine({ price: d.price, lineWidth: 1, lineStyle: 1, color: COLORS.developing,
                                                axisLabelVisible: false, title: `${d.label}?` }));
          }
        }
      }
      const zoneList = [];
      const mid = (z) => (z.zone.top + z.zone.bottom) / 2;
      if (ov && show.fvg) {
        for (const g of nearest(ov.fvg.open || [], mid)) {
          zoneList.push({ kind: "fvg", direction: g.direction, from: epoch(g.time), to: null, top: g.zone.top, bottom: g.zone.bottom,
                          label: `FVG${g.mitigation === "partially_mitigated" ? ` ${Math.round(g.mitigated_pct)}%` : ""}` });
        }
        if (show.developing) {
          for (const g of ov.fvg.developing || []) {
            zoneList.push({ kind: "fvg", direction: g.direction, developing: true, from: epoch(g.time), to: null,
                            top: g.zone.top, bottom: g.zone.bottom, label: "FVG? (forming)" });
          }
        }
      }
      if (ov && show.ob) {
        for (const o of nearest(ov.order_blocks.open || [], mid)) {
          zoneList.push({ kind: "ob", direction: o.direction, from: epoch(o.time), to: null, top: o.zone.top, bottom: o.zone.bottom,
                          label: `OB candidate${o.mitigation === "touched" ? " (touched)" : ""}` });
        }
      }
      zones.set(zoneList, firstTime);
      renderMarkers();
    },
    clearOverlays() { view.ov = null; clearLines(); zones.set([], firstTime); markers.setMarkers([]); },
    dispose() {
      if (disposed) return;
      disposed = true;
      chart.remove();
      live -= 1;
    },
  };
}

function shortLevel(l) {
  const names = { EQH: "EQH", EQL: "EQL", PDH: "PDH", PDL: "PDL", PWH: "PWH", PWL: "PWL" };
  if (names[l.type]) return names[l.type];
  if (l.session) return `${l.session} ${l.type === "session_high" ? "H" : "L"}`;
  return l.label;
}

export function createLineChart(container) {
  const chart = LWC.createChart(container, {
    autoSize: true,
    layout: { background: { type: "solid", color: "#050505" }, textColor: COLORS.text, fontSize: 11 },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    rightPriceScale: { borderColor: COLORS.border }, timeScale: { borderColor: COLORS.border, timeVisible: true },
  });
  live += 1;
  const series = chart.addSeries(LWC.LineSeries, { color: "#a78bfa", lineWidth: 2,
                                                   priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
  return { chart, series, dispose() { chart.remove(); live -= 1; } };
}

/** Several line series on one chart (equity curves per timeframe stream). Each point: {time (epoch s), value}. */
export function createMultiLineChart(container) {
  const chart = LWC.createChart(container, {
    autoSize: true,
    layout: { background: { type: "solid", color: "#050505" }, textColor: COLORS.text, fontSize: 11 },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    rightPriceScale: { borderColor: COLORS.border },
    timeScale: { borderColor: COLORS.border, timeVisible: true, tickMarkFormatter: tickMark },
    localization: { timeFormatter: crosshairTime },
  });
  live += 1;
  let series = [];
  return {
    chart,
    setSeries(list) {
      for (const s of series) chart.removeSeries(s);
      series = list.map((item) => {
        const s = chart.addSeries(LWC.LineSeries, { color: item.color, lineWidth: 2, title: item.name,
                                                    priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
        const seen = new Map();
        for (const p of item.points) seen.set(p.time, p.value);      // one value per timestamp (last wins)
        s.setData([...seen.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value })));
        return s;
      });
      chart.timeScale().fitContent();
    },
    dispose() { chart.remove(); live -= 1; },
  };
}

