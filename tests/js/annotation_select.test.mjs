// Chart annotation selection (Part 13): run with `node --test tests/js` (pytest runs it when Node exists).
import assert from "node:assert/strict";
import { test } from "node:test";

import { LABEL_BARS, SWING_BARS, selectMarkers } from "../../web/js/annotation-select.js";

const times = new Set(Array.from({ length: 100 }, (_, i) => 1000 + i * 60));
const m = (i, text) => ({ time: 1000 + i * 60, text });
const groups = [
  { kind: "events", markers: [m(10, "BOS"), m(50, "CHoCH"), m(90, "BOS")] },
  { kind: "developing", markers: [m(99, "break?")] },
  { kind: "sweeps", markers: [m(40, "sweep?"), m(95, "sweep?")] },
  { kind: "swings", markers: [m(20, "HH"), m(60, "HL"), m(80, "LH"), m(97, "LL")] },
];
const view = (over = {}) => ({ range: null, bars: 50, max: 40, lastTime: null, times, ...over });

test("keeps everything with labels when zoomed in and under the maximum", () => {
  const out = selectMarkers(groups, view());
  assert.equal(out.markers.length, 10);
  assert.equal(out.labels, true);
  assert.deepEqual(out.markers.map((x) => x.time), [...out.markers.map((x) => x.time)].sort((a, b) => a - b));
});

test("the maximum is filled by priority: breaks, then developing, then sweeps, then swings", () => {
  const out = selectMarkers(groups, view({ max: 5 }));
  assert.deepEqual(out.markers.map((x) => x.text).sort(), ["BOS", "BOS", "CHoCH", "break?", "sweep?"].sort());
  assert.ok(!out.markers.some((x) => ["HH", "HL", "LH", "LL"].includes(x.text)));
});

test("only annotations inside the visible range count", () => {
  const out = selectMarkers(groups, view({ range: { from: 1000 + 45 * 60, to: 1000 + 85 * 60 } }));
  assert.deepEqual(out.markers.map((x) => x.text), ["CHoCH", "HL", "LH"]);
});

test("labels are hidden when zoomed out, swings dropped when very zoomed out", () => {
  const out = selectMarkers(groups, view({ bars: LABEL_BARS + 1 }));
  assert.equal(out.labels, false);
  assert.ok(out.markers.every((x) => x.text === ""));
  assert.ok(out.markers.length === 10);
  const far = selectMarkers(groups, view({ bars: SWING_BARS + 1 }));
  assert.equal(far.swingsAllowed, false);
  assert.equal(far.markers.length, 6);                                  // 3 breaks + 1 developing + 2 sweeps
});

test("annotations on candles that are not on the chart, or after the last candle, are never drawn", () => {
  const extra = [{ kind: "events", markers: [{ time: 5, text: "BOS" }, m(99, "BOS")] }];
  assert.equal(selectMarkers(extra, view()).markers.length, 1);
  assert.equal(selectMarkers(extra, view({ lastTime: 1000 + 98 * 60 })).markers.length, 0);
});
