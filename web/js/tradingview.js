// Optional TradingView Advanced Chart panel. DISPLAY ONLY.
// - Loaded only on request (or if the user opted into auto-load).
// - Lives in its own document (/tv-widget) whose CSP only allows TradingView hosts,
//   so it cannot call the GoldScalp API; GoldScalp never reads the widget's data.
// - Failure is contained to this panel.
import { announce, kindBadge, storageGet, storageSet } from "./ui.js";

const AUTO_KEY = "goldscalp:tv-autoload:v1";
const LOAD_TIMEOUT_MS = 25000;
const INTERVALS = ["1", "5", "15", "60"];

export function initTradingView(config) {
  const panel = document.getElementById("tv-panel");
  if (!config || !config.tradingview_widget) return;   // disabled server-side: the panel stays hidden
  panel.hidden = false;
  document.getElementById("tv-kind").replaceWith(kindBadge("thirdparty"));
  document.getElementById("tv-symbol-label").textContent = config.tradingview_symbol;
  const gate = document.getElementById("tv-gate");
  const holder = document.getElementById("tv-holder");
  const error = document.getElementById("tv-error");
  const auto = document.getElementById("tv-auto");
  auto.checked = storageGet(AUTO_KEY) === "1";
  auto.addEventListener("change", () => storageSet(AUTO_KEY, auto.checked ? "1" : "0"));
  let frame = null;
  let interval = "5";
  let timer = null;

  const buttons = [...document.querySelectorAll("#tv-tf-group button")];
  const press = () => buttons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tvi === interval)));
  buttons.forEach((b) => b.addEventListener("click", () => {
    if (!INTERVALS.includes(b.dataset.tvi)) return;
    interval = b.dataset.tvi;
    press();
    if (frame && frame.contentWindow) frame.contentWindow.postMessage({ type: "goldscalp:tv-interval", interval }, "*");
  }));
  press();

  window.addEventListener("message", (event) => {
    if (!frame || event.source !== frame.contentWindow) return;   // only our own frame
    const data = event.data;
    if (!data || data.type !== "goldscalp:tv-status") return;
    if (data.status === "loaded") { clearTimeout(timer); announce(error, ""); }
    if (data.status === "failed") fail(`TradingView chart unavailable (${String(data.detail).slice(0, 120)}).`);
  });

  function fail(message) {
    clearTimeout(timer);
    announce(error, `${message} GoldScalp's own chart, data and simulator are unaffected.`, "error");
  }

  function load() {
    if (frame) return;
    gate.hidden = true;
    frame = document.createElement("iframe");
    frame.className = "tv-frame";
    frame.title = `TradingView chart of ${config.tradingview_symbol} (third-party, display only)`;
    frame.referrerPolicy = "no-referrer";
    frame.loading = "lazy";
    if (config.tradingview_sandbox) frame.setAttribute("sandbox", "allow-scripts allow-popups allow-popups-to-escape-sandbox");
    const params = new URLSearchParams({ symbol: config.tradingview_symbol, interval });
    frame.src = `/tv-widget?${params.toString()}`;
    frame.addEventListener("error", () => fail("The TradingView frame could not be loaded."));
    holder.append(frame);
    timer = setTimeout(() => fail("TradingView did not respond in time."), LOAD_TIMEOUT_MS);
  }

  document.getElementById("tv-load").addEventListener("click", load);
  if (auto.checked) load();
}
