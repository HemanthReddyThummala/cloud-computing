// Test doubles: an in-memory chrome API and a local HTTP fake gateway that follows docs/api.md
// (ADR-18 OAuth endpoints + POST /jobs). Nothing here talks to Notion or any external host.
import http from "node:http";

export const EXTENSION_ID = "kkkeigfecmfhmainhbkcpdhjnloifbdf";
export const REDIRECT_URI = `https://${EXTENSION_ID}.chromiumapp.org/notion`;
export const SEALED = "nva1.gAAAAABfakeSealedTokenForTestsOnly0123456789";

function storageArea() {
  const data = new Map();
  return {
    data,
    async get(key) {
      return data.has(key) ? { [key]: structuredClone(data.get(key)) } : {};
    },
    async set(items) {
      for (const [k, v] of Object.entries(items)) data.set(k, structuredClone(v));
    },
    async remove(key) {
      data.delete(key);
    },
  };
}

/** launch(details) decides what launchWebAuthFlow does; default: approve with code "good-code". */
export function fakeChrome({ launch } = {}) {
  const calls = { launch: [] };
  const chrome = {
    runtime: { id: EXTENSION_ID },
    storage: { local: storageArea(), session: storageArea() },
    identity: {
      getRedirectURL: (path = "") => `https://${EXTENSION_ID}.chromiumapp.org/${path}`,
      async launchWebAuthFlow(details) {
        calls.launch.push(details);
        if (launch) return launch(details);
        const state = new URL(details.url).searchParams.get("state");
        return `${REDIRECT_URI}?code=good-code&state=${encodeURIComponent(state)}`;
      },
    },
  };
  return { chrome, calls };
}

/**
 * Fake gateway. `behaviour` keys (functions returning {status, body, headers?, raw?} or undefined
 * for the default): authorize, callback, jobs. Records every request (method, path, body).
 */
export async function startFakeGateway(behaviour = {}) {
  const requests = [];
  const issued = new Set();
  let counter = 0;
  let jobCounter = 0;
  const byKey = new Map();      // Idempotency-Key -> {body, job}
  const statusScripts = new Map(); // job_id -> array of {status, body} (last one repeats)
  let inFlightStatus = 0;
  let maxInFlightStatus = 0;
  const server = http.createServer(async (req, res) => {
    let raw = "";
    for await (const chunk of req) raw += chunk;
    let body = null;
    try {
      body = raw ? JSON.parse(raw) : null;
    } catch {
      body = "INVALID";
    }
    const record = { method: req.method, path: req.url, body, headers: req.headers };
    requests.push(record);
    const reply = ({ status = 200, body: out = {}, headers = {}, raw: rawOut, hang = false }) => {
      if (hang) return; // never answer
      res.writeHead(status, { "Content-Type": "application/json", ...headers });
      res.end(rawOut ?? JSON.stringify(out));
    };
    const err = (status, code, headers) => reply({ status, body: { error: { code, message: "fixed" } }, headers });

    if (req.method === "POST" && req.url === "/oauth/notion/authorize") {
      const custom = behaviour.authorize?.(record);
      if (custom) return reply(custom);
      counter += 1;
      const state = `state-${counter}-abcdefghijklmnopqrstuvwxyz0123456789`;
      issued.add(state);
      const url = `https://notion.example.test/v1/oauth/authorize?client_id=cid&response_type=code&owner=user`
        + `&redirect_uri=${encodeURIComponent(REDIRECT_URI)}&state=${state}`;
      return reply({ body: { authorization_url: url, state, redirect_uri: REDIRECT_URI, expires_in: 600 } });
    }
    if (req.method === "POST" && req.url === "/oauth/notion/callback") {
      const custom = behaviour.callback?.(record);
      if (custom) return reply(custom);
      if (!body || typeof body !== "object" || typeof body.state !== "string") return err(400, "OAUTH_INVALID_REQUEST");
      if (!issued.delete(body.state)) return err(400, "OAUTH_STATE_INVALID");
      if (body.error === "access_denied") return err(400, "OAUTH_ACCESS_DENIED");
      if (body.error) return err(400, "OAUTH_AUTHORIZATION_FAILED");
      if (!body.code) return err(400, "OAUTH_CODE_MISSING");
      if (body.code === "expired") return err(400, "OAUTH_EXCHANGE_FAILED");
      if (body.code === "ratelimit") return err(503, "NOTION_RATE_LIMITED", { "Retry-After": "7" });
      return reply({ body: { notion_token: SEALED, token_format: "sealed-v1", workspace_id: "ws-456",
        workspace_name: "Test Workspace", bot_id: "bot-123" } });
    }
    if (req.method === "POST" && req.url === "/jobs") {
      const custom = behaviour.jobs?.(record);
      if (custom) return reply(custom);
      if (req.headers["content-type"] !== "application/json") return err(415, "UNSUPPORTED_MEDIA_TYPE");
      if (typeof body?.notion_token !== "string" || /\s/.test(body.notion_token)) return err(400, "INVALID_NOTION_TOKEN");
      const key = req.headers["idempotency-key"];
      if (key && byKey.has(key)) {                       // documented replay semantics (docs/api.md)
        const prior = byKey.get(key);
        if (prior.body !== raw) return err(422, "IDEMPOTENCY_KEY_REUSED");
        return reply({ status: 202, body: prior.job, headers: { "Idempotent-Replayed": "true" } });
      }
      jobCounter += 1;
      const videoId = new URL(body.youtube_url).searchParams.get("v") ?? "dQw4w9WgXcQ";
      const job = { job_id: `11111111-2222-3333-4444-${String(555555555554 + jobCounter)}`, status: "QUEUED", video_id: videoId };
      if (key) byKey.set(key, { body: raw, job });
      return reply({ status: 202, body: job });
    }
    const statusMatch = req.method === "GET" && /^\/jobs\/([^/]+)$/.exec(req.url);
    if (statusMatch) {
      const jobId = decodeURIComponent(statusMatch[1]);
      inFlightStatus += 1;
      maxInFlightStatus = Math.max(maxInFlightStatus, inFlightStatus);
      if (behaviour.statusDelayMs) await new Promise((r) => setTimeout(r, behaviour.statusDelayMs));
      inFlightStatus -= 1;
      const custom = behaviour.status?.(record, jobId);
      if (custom) return reply(custom);
      const script = statusScripts.get(jobId);
      if (!script) return err(404, "JOB_NOT_FOUND");
      const { status: http, body: fixed, raw: rawBody, ...fields } = script.length > 1 ? script.shift() : script[0];
      return reply({ status: http ?? 200, body: fixed ?? statusBody(jobId, fields), raw: rawBody });
    }
    return err(404, "NOT_FOUND");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    baseUrl: `http://127.0.0.1:${port}`,
    requests,
    paths: () => requests.map((r) => r.path),
    /** Script GET /jobs/{id}: each entry is a job status string or {status?, body?, raw?, ...fields}. */
    script: (jobId, steps) => statusScripts.set(jobId, steps.map((s) => (typeof s === "string" ? { jobStatus: s } : s))),
    statusGets: (jobId) => requests.filter((r) => r.method === "GET" && (!jobId || r.path === `/jobs/${jobId}`)).length,
    maxConcurrentStatus: () => maxInFlightStatus,
    close: () => new Promise((resolve) => {
      server.closeAllConnections();
      server.close(resolve);
    }),
  };
}

const PROGRESS = { QUEUED: 0, TRANSCRIBING: 10, SUMMARIZING: 30, EXTRACTING_FRAMES: 55, PUBLISHING: 85,
  COMPLETED: 100, COMPLETED_WITH_WARNINGS: 100 };

/** A GET /jobs/{id} body in the documented shape (docs/api.md). */
export function statusBody(jobId, { jobStatus = "QUEUED", ...extra } = {}) {
  const done = jobStatus === "COMPLETED" || jobStatus === "COMPLETED_WITH_WARNINGS";
  return {
    job_id: jobId, status: jobStatus, progress: PROGRESS[jobStatus] ?? 30, video_id: "dQw4w9WgXcQ",
    video_title: jobStatus === "QUEUED" ? null : "Test Video", duration_sec: jobStatus === "QUEUED" ? null : 212,
    warnings: [], error: null,
    result: done ? { notion_page_url: "https://www.notion.so/Test-Video-0123456789abcdef0123456789abcdef",
      frames_requested: 3, frames_published: 3 } : null,
    retry: null, created_at: "2026-11-20T10:00:00+00:00", updated_at: "2026-11-20T10:00:05+00:00",
    ...extra,
  };
}

/** Wait until fn() is truthy (polls every 5 ms). */
export async function waitFor(fn, { timeout = 3000, what = "condition" } = {}) {
  const end = Date.now() + timeout;
  for (;;) {
    const value = await fn();
    if (value) return value;
    if (Date.now() > end) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 5));
  }
}

/** A logger that records everything it is given. */
export function recordingLogger() {
  const lines = [];
  const add = (...args) => lines.push(args.map(String).join(" "));
  return { lines, info: add, warn: add, error: add, log: add };
}
