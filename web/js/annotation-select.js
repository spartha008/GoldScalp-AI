// Which chart annotations to draw (pure logic, no DOM; unit-tested with `node --test`).
// Zoom-aware: only annotations inside the visible time range count toward the maximum, text is hidden
// when many candles are visible, and swing markers are dropped when very zoomed out.

export const LABEL_BARS = 160;   // more visible candles than this: marker text is hidden
export const SWING_BARS = 300;   // more than this: swing markers are hidden

/**
 * @param {{kind: string, markers: {time: number, text: string}[]}[]} groups  in priority order
 * @param {{range: {from: number, to: number} | null, bars: number, max: number, lastTime: number | null,
 *          times: Set<number>}} view
 */
export function selectMarkers(groups, { range, bars, max, lastTime, times }) {
  const labels = bars <= LABEL_BARS;
  const swingsAllowed = bars <= SWING_BARS;
  const inView = (t) => times.has(t) && (!range || (t >= range.from && t <= range.to)) && (lastTime === null || t <= lastTime);
  const chosen = [];
  for (const group of groups) {
    if (group.kind === "swings" && !swingsAllowed) continue;
    const visible = group.markers.filter((m) => inView(m.time)).sort((a, b) => b.time - a.time);
    for (const m of visible) {
      if (chosen.length >= max) break;
      chosen.push(labels ? m : { ...m, text: "" });
    }
  }
  chosen.sort((a, b) => a.time - b.time);
  return { markers: chosen, labels, swingsAllowed };
}
