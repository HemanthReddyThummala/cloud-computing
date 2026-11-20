// Minimal client for the API gateway (docs/api.md). Called only from the background service worker,
// which has host_permissions for the gateway origin, so no CORS is involved.

export class ExtensionError extends Error {
  constructor(code, { status = null, retryAfterSec = null } = {}) {
    super(code);
    this.name = "ExtensionError";
    this.code = code;
    this.status = status;
    this.retryAfterSec = retryAfterSec;
  }
}

const GATEWAY_CODE = /^[A-Z][A-Z0-9_]{0,63}$/;

function retryAfter(headers) {
  const value = Number.parseInt(headers.get("retry-after") ?? "", 10);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

/**
 * POST (or GET) a JSON request to the gateway.
 * Resolves with the parsed JSON object on 2xx. Rejects with ExtensionError:
 *   GATEWAY_UNREACHABLE / GATEWAY_TIMEOUT       network failure / no answer in time
 *   <gateway error.code>                         any non-2xx with the documented error shape
 *   GATEWAY_INVALID_RESPONSE                     anything else (non-JSON, wrong shape)
 */
export async function callGateway(fetchImpl, baseUrl, path, { method = "POST", body, timeoutMs = 30000, headers = {} } = {}) {
  const init = { method, cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(timeoutMs), headers: { ...headers } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetchImpl(`${baseUrl}${path}`, init);
  } catch (err) {
    const timedOut = err?.name === "TimeoutError" || err?.name === "AbortError";
    throw new ExtensionError(timedOut ? "GATEWAY_TIMEOUT" : "GATEWAY_UNREACHABLE");
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  const isObject = data !== null && typeof data === "object" && !Array.isArray(data);
  if (response.ok) {
    if (!isObject) throw new ExtensionError("GATEWAY_INVALID_RESPONSE", { status: response.status });
    return data;
  }
  const code = isObject ? data.error?.code : undefined;
  if (typeof code === "string" && GATEWAY_CODE.test(code)) {
    throw new ExtensionError(code, { status: response.status, retryAfterSec: retryAfter(response.headers) });
  }
  throw new ExtensionError("GATEWAY_INVALID_RESPONSE", { status: response.status });
}
