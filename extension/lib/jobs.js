// Job submission + status polling in the background service worker (ADR-20).
//
// Tracked jobs live in chrome.storage.local["jobs"] (display data only: ids, status, progress, title,
// warnings, error, result URL). Nothing secret is stored here. The service worker can be stopped at
// any time; resume() (called whenever the worker starts) picks up every non-terminal job.
//
// Polling: one loop for all jobs (never two), GET /jobs/{id} every ACTIVE_MS while the job runs, slower
// while it waits for a scheduled retry, exponential backoff (capped) while the gateway is unreachable;
// it stops for a job when the job reaches a terminal state.

import { ExtensionError, callGateway } from "./gateway.js";

export const JOBS_KEY = "jobs";
export const PENDING_KEY = "pendingSubmissions";
export const TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED", "NOT_FOUND"]);
export const STATUSES = new Set(["QUEUED", "TRANSCRIBING", "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING",
  "COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"]);
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
const MAX_JOBS = 10;
const KEY_REUSE_MS = 60 * 60 * 1000; // well inside the gateway's IDEMPOTENCY_KEY_TTL_HOURS (24 h)
// Outcome unknown: the job may exist. The same Idempotency-Key is reused on the next click.
const UNCERTAIN = new Set(["GATEWAY_UNREACHABLE", "GATEWAY_TIMEOUT", "GATEWAY_INVALID_RESPONSE",
  "IDEMPOTENCY_CONFLICT"]);

/** The 11-character video id of a youtube.com watch URL, or null. */
export function videoIdFromUrl(href) {
  let url;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || !/(^|\.)youtube\.com$/.test(url.hostname) || url.pathname !== "/watch") return null;
  const id = url.searchParams.get("v");
  return id && VIDEO_ID.test(id) ? id : null;
}

export const watchUrl = (videoId) => `https://www.youtube.com/watch?v=${videoId}`;

const str = (v, max = 500) => (typeof v === "string" ? v.slice(0, max) : null);
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Keep only the documented fields of GET /jobs/{id} (docs/api.md). */
export function sanitizeStatus(data) {
  if (!data || typeof data !== "object" || !STATUSES.has(data.status)) return null;
  const result = data.result && typeof data.result === "object" ? {
    notionPageUrl: str(data.result.notion_page_url, 2048),
    framesRequested: num(data.result.frames_requested),
    framesPublished: num(data.result.frames_published),
  } : null;
  const error = data.error && typeof data.error === "object" ? {
    code: str(data.error.code, 64), message: str(data.error.message),
    lastErrorCode: str(data.error.last_error_code, 64), resetsAt: str(data.error.resets_at, 64),
  } : null;
  const retry = data.retry && typeof data.retry === "object" ? {
    reason: str(data.retry.reason, 64), attempt: num(data.retry.attempt), maxAttempts: num(data.retry.max_attempts),
    nextAttemptAt: str(data.retry.next_attempt_at, 64),
  } : null;
  const warnings = Array.isArray(data.warnings) ? data.warnings.slice(0, 20).filter((w) => w && typeof w === "object")
    .map((w) => ({ code: str(w.code, 64), timestampSec: num(w.timestamp_sec), reason: str(w.reason, 64) })) : [];
  return {
    status: data.status, progress: num(data.progress), title: str(data.video_title, 300),
    durationSec: num(data.duration_sec), warnings, error, result, retry,
  };
}

export function createJobTracker({
  chrome, fetch, baseUrl, auth, timeoutMs = 15000, logger = console,
  now = () => Date.now(), setTimer = setTimeout, clearTimer = clearTimeout,
  randomId = () => crypto.randomUUID(),
  activeMs = 3000, retryWaitMs = 10000, maxBackoffMs = 30000,
}) {
  // ---------------------------------------------------------------- storage (serialised writes)
  let chain = Promise.resolve();
  function mutate(key, fn) {
    const run = chain.then(async () => {
      const value = (await chrome.storage.local.get(key))[key] ?? (key === JOBS_KEY ? [] : {});
      const out = await fn(value);
      await chrome.storage.local.set({ [key]: value });
      return out;
    });
    chain = run.catch(() => {});
    return run;
  }
  async function loadJobs() {
    const jobs = (await chrome.storage.local.get(JOBS_KEY))[JOBS_KEY];
    return Array.isArray(jobs) ? jobs : [];
  }

  // ---------------------------------------------------------------- submission
  const submitting = new Map(); // videoId -> promise (double clicks share one request)

  async function submitOnce(videoId) {
    const active = (await loadJobs()).find((j) => j.videoId === videoId && !TERMINAL.has(j.status));
    if (active) return { jobId: active.jobId, status: active.status, videoId, alreadyTracking: true };
    if (!(await auth.status()).connected) throw new ExtensionError("NOT_SIGNED_IN");

    for (let attempt = 0; attempt < 2; attempt += 1) {
      const key = await mutate(PENDING_KEY, (pending) => {
        for (const [id, p] of Object.entries(pending)) if (now() - p.at > KEY_REUSE_MS) delete pending[id];
        pending[videoId] ??= { key: `vna:${videoId}:${randomId()}`, at: now() };
        return pending[videoId].key;
      });
      let job;
      try {
        job = await auth.submitJob({ youtubeUrl: watchUrl(videoId), idempotencyKey: key });
      } catch (err) {
        const code = err instanceof ExtensionError ? err.code : "INTERNAL_ERROR";
        if (!UNCERTAIN.has(code)) await mutate(PENDING_KEY, (pending) => { delete pending[videoId]; });
        if (code === "IDEMPOTENCY_KEY_REUSED" && attempt === 0) continue; // body changed (new sealed token)
        logger.warn?.(`[Video Notes Agent] job submission failed: ${code}`);
        throw err;
      }
      await mutate(PENDING_KEY, (pending) => { delete pending[videoId]; });
      await track(job, videoId);
      logger.info?.(`[Video Notes Agent] job submitted: ${job.jobId}`);
      return { jobId: job.jobId, status: job.status, videoId, alreadyTracking: false };
    }
    throw new ExtensionError("IDEMPOTENCY_KEY_REUSED");
  }

  /** Submit the video of a youtube.com watch URL. Never returns or logs the token. */
  async function submit({ youtubeUrl } = {}) {
    const videoId = videoIdFromUrl(youtubeUrl);
    if (!videoId) throw new ExtensionError("NOT_A_VIDEO");
    if (!submitting.has(videoId)) {
      submitting.set(videoId, submitOnce(videoId).finally(() => submitting.delete(videoId)));
    }
    return submitting.get(videoId);
  }

  async function track(job, videoId) {
    await mutate(JOBS_KEY, (jobs) => {
      if (jobs.some((j) => j.jobId === job.jobId)) return; // idempotent replay of an already tracked job
      const at = new Date(now()).toISOString();
      jobs.unshift({
        jobId: job.jobId, videoId, youtubeUrl: watchUrl(videoId), status: job.status ?? "QUEUED", lastStage: "QUEUED",
        progress: 0, title: null, durationSec: null, warnings: [], error: null, result: null, retry: null,
        submittedAt: at, updatedAt: at, connection: "ok", failures: 0, nextPollAt: now(),
      });
      // Keep at most MAX_JOBS, dropping the oldest finished jobs first; active jobs are never dropped.
      for (let i = jobs.length - 1; jobs.length > MAX_JOBS && i >= 0; i -= 1) {
        if (TERMINAL.has(jobs[i].status)) jobs.splice(i, 1);
      }
    });
    schedule(0);
  }

  // ---------------------------------------------------------------- polling (single loop)
  let timer = null;
  let timerDue = Infinity;
  let polling = false;

  function schedule(delayMs) {
    const due = now() + Math.max(0, delayMs);
    if (timer !== null && timerDue <= due) return; // an earlier (or equal) wake-up is already set
    if (timer !== null) clearTimer(timer);
    timerDue = due;
    timer = setTimer(() => {
      timer = null;
      timerDue = Infinity;
      tick();
    }, Math.max(0, delayMs));
  }

  async function pollOne(job) {
    let update;
    try {
      const data = await callGateway(fetch, baseUrl, `/jobs/${encodeURIComponent(job.jobId)}`, { method: "GET", timeoutMs });
      const status = sanitizeStatus(data);
      if (!status) throw new ExtensionError("GATEWAY_INVALID_RESPONSE");
      update = (j) => {
        Object.assign(j, status, { connection: "ok", failures: 0, updatedAt: new Date(now()).toISOString() });
        if (!TERMINAL.has(status.status)) j.lastStage = status.status;
        j.nextPollAt = TERMINAL.has(status.status) ? null : now() + (status.retry ? retryWaitMs : activeMs);
      };
    } catch (err) {
      const code = err instanceof ExtensionError ? err.code : "INTERNAL_ERROR";
      if (code === "JOB_NOT_FOUND") {
        update = (j) => Object.assign(j, { status: "NOT_FOUND", nextPollAt: null, connection: "ok" });
      } else {
        update = (j) => {
          j.failures = (j.failures ?? 0) + 1;
          j.connection = code === "GATEWAY_UNREACHABLE" || code === "GATEWAY_TIMEOUT" ? "unreachable" : "error";
          j.nextPollAt = now() + Math.min(maxBackoffMs, activeMs * 2 ** j.failures);
        };
        logger.warn?.(`[Video Notes Agent] status poll failed: ${code}`);
      }
    }
    await mutate(JOBS_KEY, (jobs) => {
      const target = jobs.find((j) => j.jobId === job.jobId);
      if (target && !TERMINAL.has(target.status)) update(target);
    });
  }

  async function tick() {
    if (polling) return;
    polling = true;
    try {
      const due = (await loadJobs()).filter((j) => !TERMINAL.has(j.status) && (j.nextPollAt ?? 0) <= now());
      for (const job of due) await pollOne(job); // sequential: at most one request in flight
    } finally {
      polling = false;
      const next = (await loadJobs()).filter((j) => !TERMINAL.has(j.status)).map((j) => j.nextPollAt ?? 0);
      if (next.length) schedule(Math.min(...next) - now());
    }
  }

  /** Called whenever the service worker starts: continue polling every unfinished job. */
  async function resume() {
    const active = (await loadJobs()).filter((j) => !TERMINAL.has(j.status));
    if (active.length) schedule(0);
    return active.length;
  }

  return { submit, resume, tick, list: loadJobs, _state: () => ({ timer: timer !== null, polling }) };
}
