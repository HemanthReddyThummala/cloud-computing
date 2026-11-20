// Notion sign-in through the gateway (ADR-18) + job submission with the sealed token.
//
// Flow: POST /oauth/notion/authorize -> chrome.identity.launchWebAuthFlow(authorization_url)
//       -> Notion redirects to https://<id>.chromiumapp.org/notion?code=…&state=… (or error=…)
//       -> POST /oauth/notion/callback {state, code} | {state, error}
//       -> store ONLY the sealed token (nva1.…) + non-secret workspace metadata.
//
// Never stored, logged or returned to other extension pages: the authorization code, the raw Notion
// token (the extension never sees it) and the sealed token (it leaves the service worker only in the
// body of POST /jobs).

import { ExtensionError, callGateway } from "./gateway.js";

export const AUTH_KEY = "notionAuth";            // chrome.storage.local: the connection
export const LAST_ERROR_KEY = "notionAuthError"; // chrome.storage.session: last sign-in failure {code, message}
const SEALED_PREFIX = "nva1.";
const MAX_SEALED_LENGTH = 8192;

const MESSAGES = {
  SIGN_IN_CANCELLED: "Notion sign-in was cancelled.",
  SIGN_IN_FAILED: "The Notion sign-in window could not be completed. Please try again.",
  STATE_MISMATCH: "Notion's answer did not belong to this sign-in attempt. Please try again.",
  REDIRECT_MISMATCH: "The gateway's Notion redirect URI does not match this extension. Check NOTION_REDIRECT_URI.",
  GATEWAY_UNREACHABLE: "Cannot reach the Video Notes gateway. Is the backend running?",
  GATEWAY_TIMEOUT: "The Video Notes gateway did not answer in time. Please try again.",
  GATEWAY_INVALID_RESPONSE: "The gateway sent an unexpected response.",
  NOT_SIGNED_IN: "Connect Notion first.",
  NOTION_RECONNECT_REQUIRED: "Your Notion connection is no longer valid. Please connect Notion again.",
  OAUTH_ACCESS_DENIED: "Notion access was not granted.",
  OAUTH_AUTHORIZATION_FAILED: "Notion reported an authorization error. Please try again.",
  OAUTH_STATE_INVALID: "This sign-in attempt expired or was already used. Please connect Notion again.",
  OAUTH_CODE_MISSING: "Notion did not return an authorization code. Please try again.",
  OAUTH_EXCHANGE_FAILED: "Notion did not accept the authorization. Please connect Notion again.",
  OAUTH_INVALID_REQUEST: "The sign-in request was rejected by the gateway. Please try again.",
  OAUTH_NOT_CONFIGURED: "Notion sign-in is not configured on the gateway.",
  OAUTH_CLIENT_REJECTED: "Notion rejected the gateway's integration credentials.",
  OAUTH_STATE_STORE_UNAVAILABLE: "Sign-in is temporarily unavailable. Please try again.",
  NOTION_RATE_LIMITED: "Notion is busy. Please try again shortly.",
  NOTION_UNAVAILABLE: "Notion is temporarily unavailable. Please try again.",
  NOTION_UNREACHABLE: "The gateway could not reach Notion. Please try again.",
  // Job submission (Phase C Part 3)
  NOT_A_VIDEO: "Open a YouTube video (a watch page) first.",
  INVALID_YOUTUBE_URL: "This is not a YouTube video the gateway can process.",
  INVALID_REQUEST: "The gateway rejected the request.",
  IDEMPOTENCY_CONFLICT: "The same request is still being processed. Please try again.",
  IDEMPOTENCY_KEY_REUSED: "The request could not be repeated safely. Please try again.",
  BROKER_UNAVAILABLE: "The backend queue is unavailable; nothing was submitted. Please try again.",
  DATABASE_UNAVAILABLE: "The backend database is unavailable; nothing was submitted. Please try again.",
  EXTENSION_ERROR: "The extension is not responding. Reload the page and try again.",
};

export function userMessage(code) {
  return MESSAGES[code] ?? `Something went wrong (${code}).`;
}

/** {code, message} safe to show or send to other extension pages. */
export function publicError(err) {
  const code = err instanceof ExtensionError ? err.code : "INTERNAL_ERROR";
  const out = { code, message: userMessage(code) };
  if (err instanceof ExtensionError && err.retryAfterSec !== null) out.retryAfterSec = err.retryAfterSec;
  return out;
}

function isNonEmptyString(value, max = 4096) {
  return typeof value === "string" && value.length > 0 && value.length <= max;
}

function optionalString(value) {
  return typeof value === "string" && value.length <= 512 ? value : null;
}

function isValidAuth(value) {
  return value !== null && typeof value === "object" && isNonEmptyString(value.sealedToken, MAX_SEALED_LENGTH)
    && value.sealedToken.startsWith(SEALED_PREFIX);
}

function pathOf(url) {
  return `${url.origin}${url.pathname}`;
}

/** Extract {code, state, error} from the URL Chrome returns; it must be our redirect URI. */
export function parseRedirect(redirectUrl, expectedRedirectUri) {
  let url;
  try {
    url = new URL(redirectUrl);
  } catch {
    throw new ExtensionError("SIGN_IN_FAILED");
  }
  if (pathOf(url) !== pathOf(new URL(expectedRedirectUri))) throw new ExtensionError("REDIRECT_MISMATCH");
  const get = (name) => url.searchParams.get(name);
  return { code: get("code"), state: get("state"), error: get("error") };
}

/**
 * @param {object} deps
 * @param {object} deps.chrome     chrome API (identity, storage.local, storage.session)
 * @param {Function} deps.fetch    fetch implementation
 * @param {string} deps.baseUrl    gateway base URL
 * @param {string} deps.redirectPath
 * @param {number} [deps.timeoutMs]
 * @param {object} [deps.logger]   console-like; receives only outcome codes
 */
export function createAuthService({ chrome, fetch, baseUrl, redirectPath, timeoutMs = 30000, logger = console }) {
  let inFlight = null;
  let generation = 0; // bumped by signOut so a sign-in finishing afterwards does not reconnect

  const gateway = (path, options = {}) => callGateway(fetch, baseUrl, path, { ...options, timeoutMs });

  async function readAuth() {
    const value = (await chrome.storage.local.get(AUTH_KEY))[AUTH_KEY];
    return isValidAuth(value) ? value : null;
  }

  async function status() {
    const auth = await readAuth();
    const lastError = (await chrome.storage.session.get(LAST_ERROR_KEY))[LAST_ERROR_KEY] ?? null;
    return {
      connected: auth !== null,
      workspaceName: auth?.workspaceName ?? null,
      workspaceId: auth?.workspaceId ?? null,
      connectedAt: auth?.connectedAt ?? null,
      signingIn: inFlight !== null,
      lastError,
    };
  }

  async function launch(authorizationUrl) {
    try {
      return await chrome.identity.launchWebAuthFlow({ url: authorizationUrl, interactive: true });
    } catch (err) {
      // Chrome rejects with "The user did not approve access." when the window is closed.
      const message = String(err?.message ?? err);
      throw new ExtensionError(/did not approve|cancel/i.test(message) ? "SIGN_IN_CANCELLED" : "SIGN_IN_FAILED");
    }
  }

  async function flow(startGeneration) {
    const expectedRedirect = chrome.identity.getRedirectURL(redirectPath);
    const start = await gateway("/oauth/notion/authorize");
    if (!isNonEmptyString(start.authorization_url) || !start.authorization_url.startsWith("https://")
        || !isNonEmptyString(start.state, 256) || !isNonEmptyString(start.redirect_uri)) {
      throw new ExtensionError("GATEWAY_INVALID_RESPONSE");
    }
    if (start.redirect_uri !== expectedRedirect) throw new ExtensionError("REDIRECT_MISMATCH");

    const redirectUrl = await launch(start.authorization_url);
    if (!isNonEmptyString(redirectUrl, 8192)) throw new ExtensionError("SIGN_IN_FAILED");
    const answer = parseRedirect(redirectUrl, start.redirect_uri);
    if (answer.state !== start.state) {
      // Not ours: never forward its code. A denial without our state is still reported as a denial.
      if (answer.error) {
        throw new ExtensionError(answer.error === "access_denied" ? "OAUTH_ACCESS_DENIED" : "OAUTH_AUTHORIZATION_FAILED");
      }
      throw new ExtensionError("STATE_MISMATCH");
    }
    const body = { state: answer.state };
    if (answer.error) body.error = answer.error;
    else if (answer.code) body.code = answer.code;

    const done = await gateway("/oauth/notion/callback", { body });
    if (!isNonEmptyString(done.notion_token, MAX_SEALED_LENGTH) || !done.notion_token.startsWith(SEALED_PREFIX)
        || done.token_format !== "sealed-v1") {
      throw new ExtensionError("GATEWAY_INVALID_RESPONSE"); // never store anything that is not sealed
    }
    if (startGeneration !== generation) throw new ExtensionError("SIGN_IN_CANCELLED"); // signed out meanwhile
    await chrome.storage.local.set({
      [AUTH_KEY]: {
        sealedToken: done.notion_token,
        tokenFormat: done.token_format,
        workspaceId: optionalString(done.workspace_id),
        workspaceName: optionalString(done.workspace_name),
        botId: optionalString(done.bot_id),
        connectedAt: new Date().toISOString(),
      },
    });
  }

  async function runSignIn() {
    await chrome.storage.session.remove(LAST_ERROR_KEY);
    try {
      await flow(generation);
      logger.info?.("[Video Notes Agent] Notion connected");
    } catch (err) {
      const error = publicError(err);
      logger.warn?.(`[Video Notes Agent] Notion sign-in failed: ${error.code}`);
      await chrome.storage.session.set({ [LAST_ERROR_KEY]: error });
      throw err;
    }
  }

  /** Start sign-in; a second call while one is running joins the running attempt. */
  function signIn() {
    if (inFlight === null) {
      inFlight = runSignIn().finally(() => {
        inFlight = null;
      });
    }
    return inFlight.then(status);
  }

  /** Forget the local connection (the integration stays in Notion until removed there). */
  async function signOut() {
    generation += 1;
    await chrome.storage.local.remove(AUTH_KEY);
    await chrome.storage.session.remove(LAST_ERROR_KEY);
    return status();
  }

  /** POST /jobs with the sealed token. Returns {jobId, status, videoId}; never the token. */
  async function submitJob({ youtubeUrl, notionParentId, idempotencyKey } = {}) {
    const auth = await readAuth();
    if (auth === null) throw new ExtensionError("NOT_SIGNED_IN");
    const body = { youtube_url: youtubeUrl, notion_token: auth.sealedToken };
    if (notionParentId) body.notion_parent_id = notionParentId;
    let data;
    try {
      data = await gateway("/jobs", { body, headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {} });
    } catch (err) {
      if (err instanceof ExtensionError && err.code === "INVALID_NOTION_TOKEN") {
        const current = await readAuth();
        if (current?.sealedToken === auth.sealedToken) await chrome.storage.local.remove(AUTH_KEY);
        throw new ExtensionError("NOTION_RECONNECT_REQUIRED", { status: err.status });
      }
      throw err;
    }
    if (!isNonEmptyString(data.job_id, 64)) throw new ExtensionError("GATEWAY_INVALID_RESPONSE");
    return { jobId: data.job_id, status: data.status ?? null, videoId: data.video_id ?? null };
  }

  return { status, signIn, signOut, submitJob };
}

/**
 * runtime.onMessage handler body. Accepts messages only from this extension's own pages/scripts.
 * Resolves with {ok: true, ...} or {ok: false, error: {code, message}}; returns null for messages
 * that are not ours (so the listener can return false).
 */
export function createMessageHandler(service, runtimeId) {
  const handlers = {
    "auth:status": () => service.status(),
    "auth:signIn": () => service.signIn(),
    "auth:signOut": () => service.signOut(),
    "job:submit": (msg) => service.submitJob({ youtubeUrl: msg.youtubeUrl, notionParentId: msg.notionParentId }),
  };
  return (message, sender) => {
    if (sender?.id !== runtimeId || typeof message?.type !== "string" || !(message.type in handlers)) return null;
    return Promise.resolve()
      .then(() => handlers[message.type](message))
      .then((result) => ({ ok: true, ...result }), (err) => ({ ok: false, error: publicError(err) }));
  };
}
