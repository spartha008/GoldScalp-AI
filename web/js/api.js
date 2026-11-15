// Fetch wrapper: deadline on every request, envelope validation, readable errors.
const DEFAULT_TIMEOUT_MS = 15000;

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export async function api(path, { method = "GET", body, timeoutMs = DEFAULT_TIMEOUT_MS, form } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const init = { method, signal: controller.signal, credentials: "same-origin", cache: "no-store",
                 headers: { Accept: "application/json" } };
  // Custom header required by the server on state-changing calls (blocks cross-site requests / CSRF).
  if (method !== "GET") init.headers["X-GoldScalp-Request"] = "1";
  if (form) init.body = form;
  else if (body !== undefined) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  let response;
  try {
    response = await fetch(path, init);
  } catch (err) {
    throw new ApiError(err.name === "AbortError" ? "request timed out" : "server unreachable", 0);
  } finally {
    clearTimeout(timer);
  }
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* non-JSON */ }
  if (!response.ok) {
    const detail = payload && (typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail));
    throw new ApiError(detail || `HTTP ${response.status}`, response.status);
  }
  return payload;
}

/** Latest-wins guard: only the newest call's result is applied. */
export function latestOnly(fn) {
  let seq = 0;
  return async (...args) => {
    const mine = ++seq;
    const result = await fn(...args);
    return mine === seq ? result : undefined;
  };
}
