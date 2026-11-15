// Market quote stream (Server-Sent Events) with a polling fallback.
import { api } from "./api.js";

export function subscribeQuotes(onEnvelope, onTransport) {
  let source = null;
  let pollTimer = null;
  let stopped = false;

  const poll = async () => {
    if (stopped) return;
    try { onEnvelope(await api("/api/market/quote")); onTransport("polling"); }
    catch (_) { onTransport("unreachable"); }
    pollTimer = setTimeout(poll, 5000);
  };

  if ("EventSource" in window) {
    source = new EventSource("/api/market/stream");
    source.addEventListener("quote", (event) => {
      try { onEnvelope(JSON.parse(event.data)); onTransport("stream"); } catch (_) { /* ignore malformed */ }
    });
    source.onerror = () => {
      onTransport("reconnecting");
      if (source.readyState === EventSource.CLOSED && !pollTimer) poll();
    };
  } else {
    poll();
  }
  return () => { stopped = true; if (source) source.close(); if (pollTimer) clearTimeout(pollTimer); };
}
