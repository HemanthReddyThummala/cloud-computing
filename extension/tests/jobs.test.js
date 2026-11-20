// Background job tracker (lib/jobs.js) against the local fake gateway with real fetch and real timers
// (short intervals). Covers submission, duplicate prevention, idempotent retries, polling to terminal
// states, backoff on network errors, a single polling loop, and service-worker restart recovery.
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { AUTH_KEY, createAuthService } from "../lib/auth.js";
import { JOBS_KEY, PENDING_KEY, createJobTracker, sanitizeStatus, videoIdFromUrl } from "../lib/jobs.js";
import { SEALED, fakeChrome, recordingLogger, startFakeGateway, statusBody, waitFor } from "./support.js";

const VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s";
const JOB1 = "11111111-2222-3333-4444-555555555555";
const JOB2 = "11111111-2222-3333-4444-555555555556";

let gw;
let logger;
const trackers = [];

function setupChrome({ connected = true } = {}) {
  const { chrome } = fakeChrome();
  if (connected) {
    chrome.storage.local.data.set(AUTH_KEY, { sealedToken: SEALED, tokenFormat: "sealed-v1", workspaceId: "ws",
      workspaceName: "WS", botId: "b", connectedAt: "2026-11-20T00:00:00Z" });
  }
  return chrome;
}

function tracker(chrome, overrides = {}) {
  let dead = false;
  const auth = createAuthService({ chrome, fetch, baseUrl: overrides.baseUrl ?? gw.baseUrl, redirectPath: "notion",
    timeoutMs: 1000, logger });
  const t = createJobTracker({
    chrome, fetch, baseUrl: gw.baseUrl, auth, timeoutMs: 500, logger,
    activeMs: 15, retryWaitMs: 60, maxBackoffMs: 80,
    setTimer: (fn, ms) => setTimeout(() => { if (!dead) fn(); }, ms),
    ...overrides,
  });
  t.kill = () => { dead = true; }; // simulates Chrome stopping the service worker
  trackers.push(t);
  return t;
}

const jobsIn = (chrome) => chrome.storage.local.data.get(JOBS_KEY) ?? [];
const jobIn = (chrome, id) => jobsIn(chrome).find((j) => j.jobId === id);
const posts = () => gw.requests.filter((r) => r.method === "POST" && r.path === "/jobs");

beforeEach(async () => {
  gw = await startFakeGateway();
  logger = recordingLogger();
});
afterEach(async () => {
  for (const t of trackers.splice(0)) t.kill();
  await gw.close();
});

describe("submission", () => {
  test("connected: POST /jobs with the sealed token, canonical URL and an Idempotency-Key", async () => {
    gw.script(JOB1, ["QUEUED"]);
    const chrome = setupChrome();
    const out = await tracker(chrome).submit({ youtubeUrl: VIDEO });
    assert.deepEqual(out, { jobId: JOB1, status: "QUEUED", videoId: "dQw4w9WgXcQ", alreadyTracking: false });
    const [post] = posts();
    assert.deepEqual(post.body, { youtube_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ", notion_token: SEALED });
    assert.match(post.headers["idempotency-key"], /^vna:dQw4w9WgXcQ:[0-9a-f-]{36}$/);
    const job = jobIn(chrome, JOB1);
    assert.equal(job.videoId, "dQw4w9WgXcQ");
    assert.ok(!JSON.stringify(jobsIn(chrome)).includes(SEALED), "job records never contain the token");
    assert.deepEqual(chrome.storage.local.data.get(PENDING_KEY), {});
  });

  test("Notion disconnected: NOT_SIGNED_IN, no request", async () => {
    const chrome = setupChrome({ connected: false });
    await assert.rejects(tracker(chrome).submit({ youtubeUrl: VIDEO }), { code: "NOT_SIGNED_IN" });
    assert.equal(gw.requests.length, 0);
    assert.deepEqual(jobsIn(chrome), []);
  });

  test("not a watch URL: NOT_A_VIDEO, no request", async () => {
    const t = tracker(setupChrome());
    for (const url of ["https://www.youtube.com/", "https://www.youtube.com/shorts/dQw4w9WgXcQ",
      "https://evil.example/watch?v=dQw4w9WgXcQ", "http://www.youtube.com/watch?v=dQw4w9WgXcQ", "nonsense"]) {
      await assert.rejects(t.submit({ youtubeUrl: url }), { code: "NOT_A_VIDEO" }, url);
    }
    assert.equal(gw.requests.length, 0);
  });

  test("same video while its job is active: no second job", async () => {
    gw.script(JOB1, ["TRANSCRIBING"]);
    const chrome = setupChrome();
    const t = tracker(chrome);
    await t.submit({ youtubeUrl: VIDEO });
    const again = await t.submit({ youtubeUrl: "https://www.youtube.com/watch?v=dQw4w9WgXcQ" });
    assert.deepEqual(again, { jobId: JOB1, status: again.status, videoId: "dQw4w9WgXcQ", alreadyTracking: true });
    assert.equal(posts().length, 1);
  });

  test("concurrent clicks for the same video share one request", async () => {
    gw.script(JOB1, ["QUEUED"]);
    const t = tracker(setupChrome());
    const results = await Promise.all([1, 2, 3].map(() => t.submit({ youtubeUrl: VIDEO })));
    assert.equal(posts().length, 1);
    assert.ok(results.every((r) => r.jobId === JOB1));
  });

  test("different videos in a row each get their own job (batch through the extension)", async () => {
    gw.script(JOB1, ["QUEUED"]);
    gw.script(JOB2, ["QUEUED"]);
    const chrome = setupChrome();
    const t = tracker(chrome);
    await t.submit({ youtubeUrl: VIDEO });
    await t.submit({ youtubeUrl: "https://www.youtube.com/watch?v=jNQXAC9IVRw" });
    assert.equal(posts().length, 2);
    assert.deepEqual(jobsIn(chrome).map((j) => j.jobId), [JOB2, JOB1]); // newest first
  });

  test("after a job finished, the same video can be submitted again", async () => {
    gw.script(JOB1, ["COMPLETED"]);
    gw.script(JOB2, ["QUEUED"]);
    const chrome = setupChrome();
    const t = tracker(chrome);
    await t.submit({ youtubeUrl: VIDEO });
    await waitFor(() => jobIn(chrome, JOB1)?.status === "COMPLETED", { what: "first job completed" });
    const second = await t.submit({ youtubeUrl: VIDEO });
    assert.equal(second.jobId, JOB2);
    assert.notEqual(posts()[0].headers["idempotency-key"], posts()[1].headers["idempotency-key"]);
  });

  test("network failure: error reported, key kept; the next click reuses it (no duplicate job)", async () => {
    gw.script(JOB1, ["QUEUED"]);
    const chrome = setupChrome();
    let failNext = true;
    const flaky = async (url, init) => {
      if (failNext && init?.method === "POST") {
        failNext = false;
        await fetch(url, init);               // the gateway DID create the job…
        throw new TypeError("fetch failed");  // …but the answer was lost
      }
      return fetch(url, init);
    };
    const auth = createAuthService({ chrome, fetch: flaky, baseUrl: gw.baseUrl, redirectPath: "notion", logger });
    const t = tracker(chrome, { auth });
    await assert.rejects(t.submit({ youtubeUrl: VIDEO }), { code: "GATEWAY_UNREACHABLE" });
    assert.ok(chrome.storage.local.data.get(PENDING_KEY).dQw4w9WgXcQ);
    const out = await t.submit({ youtubeUrl: VIDEO });
    assert.equal(out.jobId, JOB1);
    assert.equal(posts().length, 2);
    assert.equal(posts()[0].headers["idempotency-key"], posts()[1].headers["idempotency-key"]);
    assert.equal(jobsIn(chrome).length, 1);
  });

  test("gateway down: GATEWAY_UNREACHABLE, nothing tracked", async () => {
    const chrome = setupChrome();
    const { baseUrl } = gw;
    await gw.close();
    const auth = createAuthService({ chrome, fetch, baseUrl, redirectPath: "notion", logger });
    await assert.rejects(tracker(chrome, { auth }).submit({ youtubeUrl: VIDEO }), { code: "GATEWAY_UNREACHABLE" });
    assert.deepEqual(jobsIn(chrome), []);
    gw = await startFakeGateway();
  });

  test("validation error: reported and the key is dropped", async () => {
    await gw.close();
    gw = await startFakeGateway({ jobs: () => ({ status: 400, body: { error: { code: "INVALID_YOUTUBE_URL", message: "x" } } }) });
    const chrome = setupChrome();
    await assert.rejects(tracker(chrome).submit({ youtubeUrl: VIDEO }), { code: "INVALID_YOUTUBE_URL" });
    assert.deepEqual(chrome.storage.local.data.get(PENDING_KEY), {});
  });

  test("IDEMPOTENCY_KEY_REUSED (token changed after reconnect): retried once with a fresh key", async () => {
    gw.script(JOB1, ["QUEUED"]);
    const chrome = setupChrome();
    chrome.storage.local.data.set(PENDING_KEY, { dQw4w9WgXcQ: { key: "vna:dQw4w9WgXcQ:old", at: Date.now() } });
    let first = true;
    await gw.close();
    gw = await startFakeGateway({
      jobs: () => {
        if (!first) return undefined;
        first = false;
        return { status: 422, body: { error: { code: "IDEMPOTENCY_KEY_REUSED", message: "x" } } };
      },
    });
    gw.script(JOB1, ["QUEUED"]);
    const out = await tracker(chrome).submit({ youtubeUrl: VIDEO });
    assert.equal(out.jobId, JOB1);
    assert.equal(posts()[0].headers["idempotency-key"], "vna:dQw4w9WgXcQ:old");
    assert.notEqual(posts()[1].headers["idempotency-key"], "vna:dQw4w9WgXcQ:old");
  });

  test("logs and responses never contain the sealed token", async () => {
    gw.script(JOB1, ["COMPLETED"]);
    const chrome = setupChrome();
    const out = await tracker(chrome).submit({ youtubeUrl: VIDEO });
    await waitFor(() => jobIn(chrome, JOB1)?.status === "COMPLETED");
    assert.ok(!JSON.stringify(out).includes(SEALED));
    assert.ok(!logger.lines.join("\n").includes(SEALED));
  });
});

describe("status polling", () => {
  test("follows the stages to COMPLETED and stops polling", async () => {
    gw.script(JOB1, ["QUEUED", "TRANSCRIBING", "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING", "COMPLETED"]);
    const chrome = setupChrome();
    const seen = new Set();
    const origSet = chrome.storage.local.set.bind(chrome.storage.local);
    chrome.storage.local.set = async (items) => {
      for (const j of items[JOBS_KEY] ?? []) seen.add(j.status);
      return origSet(items);
    };
    await tracker(chrome).submit({ youtubeUrl: VIDEO });
    const job = await waitFor(() => (jobIn(chrome, JOB1)?.status === "COMPLETED" ? jobIn(chrome, JOB1) : null));
    for (const s of ["QUEUED", "TRANSCRIBING", "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING", "COMPLETED"]) {
      assert.ok(seen.has(s), s);
    }
    assert.equal(job.progress, 100);
    assert.equal(job.title, "Test Video");
    assert.equal(job.result.notionPageUrl, "https://www.notion.so/Test-Video-0123456789abcdef0123456789abcdef");
    assert.equal(job.nextPollAt, null);
    const gets = gw.statusGets(JOB1);
    await new Promise((r) => setTimeout(r, 120));
    assert.equal(gw.statusGets(JOB1), gets, "no polling after a terminal state");
  });

  test("COMPLETED_WITH_WARNINGS keeps result and warnings", async () => {
    gw.script(JOB1, [{ jobStatus: "COMPLETED_WITH_WARNINGS",
      result: { notion_page_url: "https://www.notion.so/P-0123", frames_requested: 3, frames_published: 2 },
      warnings: [{ code: "FRAME_EXTRACTION_FAILED", timestamp_sec: 461.0, reason: "TIMESTAMP_OUT_OF_RANGE" }] }]);
    const chrome = setupChrome();
    await tracker(chrome).submit({ youtubeUrl: VIDEO });
    const job = await waitFor(() => (jobIn(chrome, JOB1)?.status === "COMPLETED_WITH_WARNINGS" ? jobIn(chrome, JOB1) : null));
    assert.deepEqual(job.warnings, [{ code: "FRAME_EXTRACTION_FAILED", timestampSec: 461, reason: "TIMESTAMP_OUT_OF_RANGE" }]);
    assert.deepEqual(job.result, { notionPageUrl: "https://www.notion.so/P-0123", framesRequested: 3, framesPublished: 2 });
  });

  test("FAILED and DEAD_LETTERED keep the error and the stage reached", async () => {
    gw.script(JOB1, ["SUMMARIZING", { jobStatus: "FAILED", error: { code: "VIDEO_TOO_LONG", message: "Too long." } }]);
    gw.script(JOB2, [{ jobStatus: "DEAD_LETTERED", error: { code: "RETRIES_EXHAUSTED", message: "x",
      last_error_code: "GEMINI_QUOTA_EXHAUSTED", attempts: 5, resets_at: "2026-11-21T00:00:00-08:00" } }]);
    const chrome = setupChrome();
    const t = tracker(chrome);
    await t.submit({ youtubeUrl: VIDEO });
    await t.submit({ youtubeUrl: "https://www.youtube.com/watch?v=jNQXAC9IVRw" });
    const failed = await waitFor(() => (jobIn(chrome, JOB1)?.status === "FAILED" ? jobIn(chrome, JOB1) : null));
    assert.equal(failed.error.code, "VIDEO_TOO_LONG");
    assert.equal(failed.lastStage, "SUMMARIZING");
    assert.equal(failed.result, null);
    const dead = await waitFor(() => (jobIn(chrome, JOB2)?.status === "DEAD_LETTERED" ? jobIn(chrome, JOB2) : null));
    assert.equal(dead.error.lastErrorCode, "GEMINI_QUOTA_EXHAUSTED");
  });

  test("a job waiting for a retry is polled more slowly but still followed", async () => {
    gw.script(JOB1, [{ jobStatus: "PUBLISHING", retry: { reason: "NOTION_RATE_LIMITED", attempt: 1, max_attempts: 5,
      next_attempt_at: "2026-11-20T10:01:00+00:00" } }]);
    const chrome = setupChrome();
    await tracker(chrome).submit({ youtubeUrl: VIDEO });
    const job = await waitFor(() => (jobIn(chrome, JOB1)?.retry ? jobIn(chrome, JOB1) : null));
    assert.equal(job.retry.reason, "NOTION_RATE_LIMITED");
    const at = Date.now();
    assert.ok(job.nextPollAt - at > 15, "slower than the active interval");
  });

  test("network errors while polling: backoff, 'unreachable' shown, then recovery", async () => {
    let down = 3;
    await gw.close();
    gw = await startFakeGateway({
      status: () => {
        if (down > 0) {
          down -= 1;
          return { status: 502, raw: "Bad Gateway" };
        }
        return undefined;
      },
    });
    gw.script(JOB1, ["COMPLETED"]);
    const chrome = setupChrome();
    await tracker(chrome).submit({ youtubeUrl: VIDEO });
    await waitFor(() => (jobIn(chrome, JOB1)?.failures ?? 0) >= 2, { what: "failures counted" });
    assert.equal(jobIn(chrome, JOB1).connection, "error");
    await waitFor(() => jobIn(chrome, JOB1)?.status === "COMPLETED", { what: "recovered" });
    assert.equal(jobIn(chrome, JOB1).connection, "ok");
    assert.equal(jobIn(chrome, JOB1).failures, 0);
  });

  test("gateway unreachable while polling -> connection 'unreachable', polling continues", async () => {
    gw.script(JOB1, ["QUEUED"]);
    const chrome = setupChrome();
    const t = tracker(chrome);
    await t.submit({ youtubeUrl: VIDEO });
    await gw.close();
    await waitFor(() => jobIn(chrome, JOB1)?.connection === "unreachable", { what: "unreachable" });
    const failures = jobIn(chrome, JOB1).failures;
    await waitFor(() => jobIn(chrome, JOB1).failures > failures, { what: "keeps retrying" });
    assert.ok(jobIn(chrome, JOB1).nextPollAt - Date.now() <= 80, "backoff capped");
    gw = await startFakeGateway();
  });

  test("unknown job (404) -> NOT_FOUND, polling stops", async () => {
    const chrome = setupChrome();
    await tracker(chrome).submit({ youtubeUrl: VIDEO }); // no script -> 404 JOB_NOT_FOUND
    await waitFor(() => jobIn(chrome, JOB1)?.status === "NOT_FOUND");
    const gets = gw.statusGets(JOB1);
    await new Promise((r) => setTimeout(r, 80));
    assert.equal(gw.statusGets(JOB1), gets);
  });

  test("invalid status response is ignored and polling continues", async () => {
    gw.script(JOB1, [{ body: { status: "EXPLODED" } }, "COMPLETED"]);
    const chrome = setupChrome();
    await tracker(chrome).submit({ youtubeUrl: VIDEO });
    await waitFor(() => jobIn(chrome, JOB1)?.status === "COMPLETED");
  });

  test("one polling loop: repeated resume() calls never poll in parallel", async () => {
    await gw.close();
    gw = await startFakeGateway({ statusDelayMs: 20 });
    gw.script(JOB1, ["TRANSCRIBING"]);
    gw.script(JOB2, ["SUMMARIZING"]);
    const chrome = setupChrome();
    const t = tracker(chrome);
    await t.submit({ youtubeUrl: VIDEO });
    await t.submit({ youtubeUrl: "https://www.youtube.com/watch?v=jNQXAC9IVRw" });
    for (let i = 0; i < 10; i += 1) t.resume();
    await new Promise((r) => setTimeout(r, 200));
    assert.equal(gw.maxConcurrentStatus(), 1);
    assert.ok(gw.statusGets() <= 2 * Math.ceil(200 / 35) + 4, `bounded polling (${gw.statusGets()} GETs)`);
  });
});

describe("service-worker restart", () => {
  test("a new worker resumes unfinished jobs from storage and completes them", async () => {
    gw.script(JOB1, ["TRANSCRIBING", "TRANSCRIBING", "SUMMARIZING", "PUBLISHING", "COMPLETED"]);
    const chrome = setupChrome();
    const first = tracker(chrome);
    await first.submit({ youtubeUrl: VIDEO });
    await waitFor(() => jobIn(chrome, JOB1)?.status === "TRANSCRIBING");
    first.kill(); // Chrome stops the worker: timers are gone, storage stays

    const second = tracker(chrome);
    assert.equal(await second.resume(), 1);
    await waitFor(() => jobIn(chrome, JOB1)?.status === "COMPLETED", { what: "completed after restart" });
    assert.equal(jsonCount(jobsIn(chrome)), 1);
  });

  test("resume() with only finished jobs does nothing", async () => {
    const chrome = setupChrome();
    chrome.storage.local.data.set(JOBS_KEY, [{ jobId: JOB1, videoId: "dQw4w9WgXcQ", status: "COMPLETED", nextPollAt: null }]);
    const t = tracker(chrome);
    assert.equal(await t.resume(), 0);
    await new Promise((r) => setTimeout(r, 40));
    assert.equal(gw.requests.length, 0);
  });

  test("a submit after restart still sees the active job (no duplicate)", async () => {
    gw.script(JOB1, ["SUMMARIZING"]);
    const chrome = setupChrome();
    const first = tracker(chrome);
    await first.submit({ youtubeUrl: VIDEO });
    first.kill();
    const again = await tracker(chrome).submit({ youtubeUrl: VIDEO });
    assert.equal(again.alreadyTracking, true);
    assert.equal(posts().length, 1);
  });
});

function jsonCount(list) {
  return new Set(list.map((j) => j.jobId)).size;
}

test("history is capped at 10 jobs, dropping the oldest finished ones", async () => {
  const chrome = setupChrome();
  const done = Array.from({ length: 10 }, (_, i) => ({ jobId: `old-${i}`, videoId: `vid${String(i).padStart(8, "0")}`, status: "COMPLETED" }));
  chrome.storage.local.data.set(JOBS_KEY, done);
  gw.script(JOB1, ["QUEUED"]);
  await tracker(chrome).submit({ youtubeUrl: VIDEO });
  const ids = jobsIn(chrome).map((j) => j.jobId);
  assert.equal(ids.length, 10);
  assert.equal(ids[0], JOB1);
  assert.ok(!ids.includes("old-9"));
});

test("helpers: videoIdFromUrl and sanitizeStatus", () => {
  assert.equal(videoIdFromUrl("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=x"), "dQw4w9WgXcQ");
  assert.equal(videoIdFromUrl("https://m.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ");
  assert.equal(videoIdFromUrl("https://www.youtube.com/watch?v=tooshort"), null);
  assert.equal(sanitizeStatus({ status: "NOPE" }), null);
  const s = sanitizeStatus(statusBody("j", { jobStatus: "TRANSCRIBING", extra_field: "ignored" }));
  assert.equal(s.status, "TRANSCRIBING");
  assert.equal(s.extra_field, undefined);
});
