// Shared UI helpers. DOM is built with createElement/textContent only - never innerHTML with data.

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = String(value);
    else if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "href") node.setAttribute("href", safeHref(value));
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** Only https:, http://localhost and same-origin relative links are allowed (no javascript: etc.). */
export function safeHref(value) {
  const text = String(value || "");
  if (text.startsWith("/") && !text.startsWith("//")) return text;
  try {
    const url = new URL(text);
    if (url.protocol === "https:" || (url.protocol === "http:" && url.hostname === "localhost")) return url.href;
  } catch (_) { /* fall through */ }
  return "#";
}

export function setText(id, value) {
  const node = typeof id === "string" ? document.getElementById(id) : id;
  if (node) node.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function num(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function fmtPrice(value, digits = 2) {
  const n = num(value);
  return n === null ? "—" : n.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtMoney(value, { sign = false } = {}) {
  const n = num(value);
  if (n === null) return "—";
  const text = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (sign) return (n > 0 ? "+" : n < 0 ? "−" : "") + "$" + text;
  return (n < 0 ? "−$" : "$") + text;
}

export function fmtLots(value) {
  const n = num(value);
  return n === null ? "—" : n.toFixed(2);
}

// Times are stored and sent as UTC; they are DISPLAYED in the configured zone (GOLDSCALP_DISPLAY_TIMEZONE).
let displayZoneName = "UTC";

export function setDisplayZone(zone) {
  try {
    new Intl.DateTimeFormat("en-GB", { timeZone: zone });
    displayZoneName = zone;
  } catch (_) { displayZoneName = "UTC"; }
}

export function displayZone() { return displayZoneName; }

export function fmtTime(iso, withDate = false) {
  if (!iso) return "—";
  const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const opts = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: displayZoneName,
                 timeZoneName: "short" };
  if (withDate) Object.assign(opts, { year: "numeric", month: "short", day: "2-digit" });
  return d.toLocaleString("en-GB", opts);
}

export function fmtAge(seconds) {
  const s = num(seconds);
  if (s === null) return "—";
  if (s < 60) return `${Math.max(0, Math.round(s))} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} h ago`;
  return `${(s / 86400).toFixed(1)} d ago`;
}

export function pnlClass(value) {
  const n = num(value);
  return n === null || n === 0 ? "" : n > 0 ? "up" : "down";
}

export function stateBadge(state, label) {
  const s = ["LIVE", "DELAYED", "HISTORICAL", "DISCONNECTED"].includes(state) ? state : "DISCONNECTED";
  return el("span", { class: `badge state-${s}`, role: "status", "aria-label": `Market data state: ${s}` },
    [el("span", { class: "dot", "aria-hidden": "true" }), label || s]);
}

export function kindBadge(kind) {
  const labels = { current: "Current market data", historical: "Historical", simulated: "Simulated", thirdparty: "Third-party display" };
  return el("span", { class: `badge kind-${kind}`, title: labels[kind] || kind }, [labels[kind] || kind]);
}

export function announce(node, message, tone = "") {
  if (!node) return;
  node.className = `notice ${tone}`.trim();
  node.hidden = !message;
  node.textContent = message || "";
}

export function storageGet(key, fallback = null) {
  try { const v = window.localStorage.getItem(key); return v === null ? fallback : v; } catch (_) { return fallback; }
}

export function storageSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (_) { /* storage may be unavailable */ }
}
