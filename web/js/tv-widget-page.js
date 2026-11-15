"use strict";
(function () {
// Classic script (not a module): in the opaque-origin sandbox a module script would need CORS.
// Runs INSIDE the sandboxed TradingView frame. It only configures the official widget.
// It never reads widget data and never calls the GoldScalp API.
const WIDGET_SRC = "https://widgets.tradingview-widget.com/w/en/tv-advanced-chart.js";
const INTERVALS = new Set(["1", "5", "15", "60"]);
const SYMBOL_RE = /^[A-Z0-9_]{1,20}:[A-Z0-9_.]{1,20}$/;
const LOAD_TIMEOUT_MS = 20000;

const params = new URLSearchParams(location.search);
const symbol = SYMBOL_RE.test(params.get("symbol") || "") ? params.get("symbol") : "OANDA:XAUUSD";
const interval = INTERVALS.has(params.get("interval")) ? params.get("interval") : "5";
const chart = document.getElementById("tv-chart");
chart.setAttribute("symbol", symbol);
chart.setAttribute("interval", interval);
chart.setAttribute("theme", "dark");

let settled = false;
function report(status, detail) {
  if (settled && status !== "interval") return;
  if (status === "loaded" || status === "failed") settled = true;
  // Opaque-origin frame: the parent checks event.source, not the origin.
  window.parent.postMessage({ type: "goldscalp:tv-status", status, detail: String(detail || "") }, "*");
}

function fail(reason) {
  document.getElementById("tv-fail").hidden = false;
  report("failed", reason);
}

const timer = setTimeout(() => fail("timed out waiting for the TradingView script"), LOAD_TIMEOUT_MS);
import(WIDGET_SRC)
  .then(() => customElements.whenDefined("tv-advanced-chart"))
  .then(() => { clearTimeout(timer); report("loaded", symbol); })
  .catch((err) => { clearTimeout(timer); fail(err && err.message ? err.message : "script failed to load"); });

// Only accept interval changes from the embedding dashboard, and only allow-listed values.
window.addEventListener("message", (event) => {
  if (event.source !== window.parent) return;
  const data = event.data;
  if (data && data.type === "goldscalp:tv-interval" && INTERVALS.has(data.interval)) {
    chart.setAttribute("interval", data.interval);
    report("interval", data.interval);
  }
});

})();
