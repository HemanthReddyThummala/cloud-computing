// OPTIONAL cross-check (not part of `npm test`): drives the extension's real lib/auth.js against a REAL
// gateway that uses a FAKE Notion token endpoint (backend/tests/support/fake_notion.py). Chrome's
// launchWebAuthFlow is simulated: Notion's approval is replaced by a redirect carrying code "good-…".
// Never point this at a gateway configured with real Notion credentials.
//
// Usage: GATEWAY=http://127.0.0.1:18080 node tests/live-gateway.check.js
import assert from "node:assert/strict";

import { createAuthService } from "../lib/auth.js";
import { REDIRECT_URI, fakeChrome, recordingLogger } from "./support.js";

const baseUrl = process.env.GATEWAY;
if (!baseUrl) throw new Error("set GATEWAY to the throwaway gateway's base URL");

// The fake Notion base URL is http://; the extension (correctly) only opens https:// authorization
// URLs, so the harness upgrades the scheme of that one field. Nothing loads the URL.
const fetchForHarness = async (url, init) => {
  const response = await fetch(url, init);
  if (!String(url).endsWith("/oauth/notion/authorize") || !response.ok) return response;
  const data = await response.json();
  data.authorization_url = data.authorization_url.replace(/^http:/, "https:");
  return new Response(JSON.stringify(data), { status: response.status, headers: response.headers });
};

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push(`PASS ${name}`);
  } catch (err) {
    results.push(`FAIL ${name}: ${err.code ?? err.message}`);
  }
}

const logger = recordingLogger();
const approve = (code) => async ({ url }) => `${REDIRECT_URI}?code=${code}&state=${new URL(url).searchParams.get("state")}`;
const make = (chrome) => createAuthService({ chrome, fetch: fetchForHarness, baseUrl, redirectPath: "notion", timeoutMs: 30000, logger });

await check("sign-in stores a sealed token from the real gateway", async () => {
  const { chrome } = fakeChrome({ launch: approve("good-e2e-1") });
  const status = await make(chrome).signIn();
  assert.equal(status.connected, true);
  assert.equal(status.workspaceName, "Test Workspace");
  const auth = chrome.storage.local.data.get("notionAuth");
  assert.ok(auth.sealedToken.startsWith("nva1."));
  assert.ok(!JSON.stringify(auth).includes("ntn_fake_access_"), "raw token never reaches the extension");
});

await check("POST /jobs accepts the sealed token (gateway unseals it) -> 202", async () => {
  const { chrome } = fakeChrome({ launch: approve("good-e2e-2") });
  const auth = make(chrome);
  await auth.signIn();
  const job = await auth.submitJob({ youtubeUrl: "https://www.youtube.com/watch?v=dQw4w9WgXcQ" });
  assert.match(job.jobId, /^[0-9a-f-]{36}$/);
  assert.equal(job.status, "QUEUED");
});

await check("tampered sealed token -> NOTION_RECONNECT_REQUIRED and connection cleared", async () => {
  const { chrome } = fakeChrome({ launch: approve("good-e2e-3") });
  const auth = make(chrome);
  await auth.signIn();
  const stored = chrome.storage.local.data.get("notionAuth");
  stored.sealedToken = `${stored.sealedToken.slice(0, -6)}AAAAAA`;
  await assert.rejects(auth.submitJob({ youtubeUrl: "https://youtu.be/dQw4w9WgXcQ" }), { code: "NOTION_RECONNECT_REQUIRED" });
  assert.equal(chrome.storage.local.data.get("notionAuth"), undefined);
});

await check("denial -> real gateway OAUTH_ACCESS_DENIED", async () => {
  const { chrome } = fakeChrome({ launch: async ({ url }) => `${REDIRECT_URI}?error=access_denied&state=${new URL(url).searchParams.get("state")}` });
  await assert.rejects(make(chrome).signIn(), { code: "OAUTH_ACCESS_DENIED" });
});

await check("expired / rejected code -> real gateway OAUTH_EXCHANGE_FAILED", async () => {
  const { chrome } = fakeChrome({ launch: approve("expired") });
  await assert.rejects(make(chrome).signIn(), { code: "OAUTH_EXCHANGE_FAILED" });
});

await check("missing code -> real gateway OAUTH_CODE_MISSING", async () => {
  const { chrome } = fakeChrome({ launch: async ({ url }) => `${REDIRECT_URI}?state=${new URL(url).searchParams.get("state")}` });
  await assert.rejects(make(chrome).signIn(), { code: "OAUTH_CODE_MISSING" });
});

await check("reused state -> real gateway OAUTH_STATE_INVALID", async () => {
  let first = null;
  const { chrome } = fakeChrome({
    launch: async ({ url }) => {
      first ??= new URL(url).searchParams.get("state");
      return `${REDIRECT_URI}?code=good-e2e-4&state=${first}`;
    },
  });
  const auth = make(chrome);
  await auth.signIn();
  // Second attempt gets a new state from the gateway; the harness replays the first one, which the
  // extension refuses locally (STATE_MISMATCH) before the gateway is asked.
  await assert.rejects(auth.signIn(), { code: "STATE_MISMATCH" });
  const res = await fetch(`${baseUrl}/oauth/notion/callback`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ state: first, code: "good-e2e-5" }),
  });
  assert.equal((await res.json()).error.code, "OAUTH_STATE_INVALID");
});

// ---------------------------------------------------------------- Phase C Part 3: tracker + real API
const { createJobTracker, JOBS_KEY } = await import("../lib/jobs.js");

await check("tracker: submit with Idempotency-Key, replay gives the same job, real GET /jobs/{id} is parsed", async () => {
  const { chrome } = fakeChrome({ launch: approve("good-e2e-6") });
  const auth = make(chrome);
  await auth.signIn();
  const tracker = createJobTracker({ chrome, fetch: fetchForHarness, baseUrl, auth, logger, setTimer: () => 0, clearTimer: () => {} });
  const job = await tracker.submit({ youtubeUrl: "https://www.youtube.com/watch?v=jNQXAC9IVRw&t=3s" });
  assert.equal(job.alreadyTracking, false);
  // Same key + same body again (what a retry after a lost response does) -> the gateway replays the job.
  const replay = await auth.submitJob({ youtubeUrl: "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    idempotencyKey: "vna:jNQXAC9IVRw:e2e-fixed-key" });
  const replay2 = await auth.submitJob({ youtubeUrl: "https://www.youtube.com/watch?v=jNQXAC9IVRw",
    idempotencyKey: "vna:jNQXAC9IVRw:e2e-fixed-key" });
  assert.equal(replay.jobId, replay2.jobId);
  // Poll the real status endpoint (no workers run here, so the job stays QUEUED).
  await tracker.tick();
  const [tracked] = chrome.storage.local.data.get(JOBS_KEY);
  assert.equal(tracked.jobId, job.jobId);
  assert.equal(tracked.status, "QUEUED");
  assert.equal(tracked.connection, "ok");
  assert.equal(tracked.progress, 0);
  assert.ok(!JSON.stringify(chrome.storage.local.data.get(JOBS_KEY)).includes("nva1."));
});

await check("tracker: unknown job id -> real 404 JOB_NOT_FOUND -> NOT_FOUND", async () => {
  const { chrome } = fakeChrome();
  chrome.storage.local.data.set(JOBS_KEY, [{ jobId: "00000000-0000-4000-8000-000000000000", videoId: "jNQXAC9IVRw",
    status: "QUEUED", nextPollAt: 0, failures: 0 }]);
  const tracker = createJobTracker({ chrome, fetch: fetchForHarness, baseUrl, auth: make(chrome), logger, setTimer: () => 0, clearTimer: () => {} });
  await tracker.tick();
  assert.equal(chrome.storage.local.data.get(JOBS_KEY)[0].status, "NOT_FOUND");
});

const logText = logger.lines.join("\n");
results.push(/good-e2e|ntn_fake_access_|nva1\./.test(logText) ? "FAIL extension logs contain a secret" : "PASS extension logs contain no code/token");
console.log(results.join("\n"));
process.exitCode = results.some((r) => r.startsWith("FAIL")) ? 1 : 0;
