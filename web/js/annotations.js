// Chart annotation settings, saved PER TIMEFRAME in this browser (Part 13).
// M1 defaults are deliberately sparse so the 1-minute chart stays readable.
import { storageGet, storageSet } from "./ui.js";

export const LAYERS = ["structure", "swings", "liquidity", "sweeps", "fvg", "ob", "developing"];

export const ANNOT_DEFAULTS = {
  M1: { structure: true, swings: false, liquidity: true, sweeps: false, fvg: false, ob: false, developing: false, max: 6, sensitivity: "" },
  M5: { structure: true, swings: true, liquidity: true, sweeps: true, fvg: true, ob: false, developing: true, max: 10, sensitivity: "" },
  M15: { structure: true, swings: true, liquidity: true, sweeps: true, fvg: true, ob: true, developing: true, max: 12, sensitivity: "" },
  H1: { structure: true, swings: true, liquidity: true, sweeps: true, fvg: true, ob: true, developing: true, max: 12, sensitivity: "" },
};

const key = (prefix, tf) => `goldscalp:${prefix}:${tf}:v1`;

export function loadAnnotations(tf, prefix = "annot") {
  const base = { ...(ANNOT_DEFAULTS[tf] || ANNOT_DEFAULTS.M5) };
  try {
    const saved = JSON.parse(storageGet(key(prefix, tf), "null"));
    if (saved && typeof saved === "object") {
      for (const name of LAYERS) if (typeof saved[name] === "boolean") base[name] = saved[name];
      if (Number.isInteger(saved.max) && saved.max >= 2 && saved.max <= 60) base.max = saved.max;
      if (saved.sensitivity === "" || /^(?:[1-9]|10)$/.test(String(saved.sensitivity))) base.sensitivity = String(saved.sensitivity);
    }
  } catch (_) { /* defaults */ }
  return base;
}

export function saveAnnotations(tf, settings, prefix = "annot") {
  storageSet(key(prefix, tf), JSON.stringify(settings));
}

/** Wire the controls (ids: ov-<layer>, ov-max, ov-sens). ``getTf`` returns the current timeframe;
 *  ``onChange(settings, {sensitivityChanged})`` is called after every change. Returns {sync(tf)}. */
export function bindAnnotationControls(getTf, onChange, prefix = "annot") {
  const box = (name) => document.getElementById(`ov-${name}`);
  const maxSel = document.getElementById("ov-max");
  const sensSel = document.getElementById("ov-sens");
  const read = () => {
    const s = {};
    for (const name of LAYERS) s[name] = Boolean(box(name) && box(name).checked);
    s.max = Number(maxSel.value) || 10;
    s.sensitivity = sensSel.value;
    return s;
  };
  const sync = (tf) => {
    const s = loadAnnotations(tf, prefix);
    for (const name of LAYERS) if (box(name)) box(name).checked = s[name];
    maxSel.value = String(s.max);
    if (![...maxSel.options].some((o) => o.value === String(s.max))) maxSel.value = "10";
    sensSel.value = s.sensitivity;
    return s;
  };
  let lastSens = null;
  const changed = () => {
    const s = read();
    saveAnnotations(getTf(), s, prefix);
    const sensitivityChanged = lastSens !== null && lastSens !== s.sensitivity;
    lastSens = s.sensitivity;
    onChange(s, { sensitivityChanged });
  };
  for (const name of LAYERS) if (box(name)) box(name).addEventListener("change", changed);
  maxSel.addEventListener("change", changed);
  sensSel.addEventListener("change", changed);
  return {
    sync(tf) { const s = sync(tf); lastSens = s.sensitivity; return s; },
    read,
  };
}
