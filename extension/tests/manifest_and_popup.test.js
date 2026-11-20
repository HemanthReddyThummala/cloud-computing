// Manifest permissions/wiring and the popup's view model.
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { test } from "node:test";

import { GATEWAY_BASE_URL, NOTION_REDIRECT_PATH } from "../config.js";
import { describeConnection, describeJob, reasonText, safeNotionUrl } from "../lib/popup-view.js";

const root = new URL("../", import.meta.url);
const manifest = JSON.parse(readFileSync(new URL("manifest.json", root), "utf8"));

test("manifest: only the permissions the sign-in needs", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual(manifest.permissions, ["identity", "storage"]);
  assert.deepEqual(manifest.host_permissions, ["http://localhost:8000/*"]);
  assert.equal(manifest.externally_connectable, undefined);
  assert.equal(manifest.optional_permissions, undefined);
});

test("manifest: the content script runs only on YouTube (no extra host permission)", () => {
  assert.deepEqual(manifest.content_scripts, [{ matches: ["https://www.youtube.com/*"], js: ["content.js"],
    css: ["content.css"], run_at: "document_idle" }]);
  for (const file of ["content.js", "content.css", "lib/jobs.js"]) assert.ok(existsSync(new URL(file, root)), file);
});

test("manifest: host permission covers the configured gateway", () => {
  const origin = new URL(GATEWAY_BASE_URL).origin;
  assert.ok(manifest.host_permissions.includes(`${origin}/*`));
  assert.equal(NOTION_REDIRECT_PATH, "notion");
});

test("manifest: key unchanged (stable extension ID) and module service worker + popup wired", () => {
  assert.ok(manifest.key.startsWith("MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAj2/Dvr7890CnV+pJRTS7"));
  assert.deepEqual(manifest.background, { service_worker: "background.js", type: "module" });
  assert.equal(manifest.action.default_popup, "popup.html");
  for (const file of ["background.js", "popup.html", "popup.js", "popup.css", "config.js", "lib/auth.js",
    "lib/gateway.js", "lib/popup-view.js"]) {
    assert.ok(existsSync(new URL(file, root)), file);
  }
});

test("popup view: connected / not connected / signing in / error", () => {
  assert.deepEqual(describeConnection({ ok: true, connected: true, workspaceName: "WS" }).button,
    { action: "auth:signOut", label: "Disconnect" });
  assert.equal(describeConnection({ ok: true, connected: true, workspaceName: "WS" }).text, "Connected to Notion: WS");
  const idle = describeConnection({ ok: true, connected: false, lastError: null });
  assert.equal(idle.button.action, "auth:signIn");
  assert.equal(idle.tone, "idle");
  const failed = describeConnection({ ok: true, connected: false, lastError: { code: "X", message: "Notion access was not granted." } });
  assert.equal(failed.detail, "Notion access was not granted.");
  assert.equal(failed.tone, "error");
  assert.equal(describeConnection({ ok: true, signingIn: true }).button, null);
  assert.equal(describeConnection({ ok: false, error: { message: "down" } }).detail, "down");
});

// ------------------------------------------------------------------ popup: job progress
const base = { jobId: "j", videoId: "dQw4w9WgXcQ", title: "Test Video", progress: 30, warnings: [], error: null,
  result: null, retry: null, connection: "ok", lastStage: "QUEUED" };
const stepStates = (view) => view.steps.map((s) => `${s.label}:${s.state}`);

test("popup job: human-readable stage sequence while processing", () => {
  const view = describeJob({ ...base, status: "SUMMARIZING", lastStage: "SUMMARIZING" });
  assert.deepEqual(stepStates(view), ["Queued:done", "Transcribing:done", "Summarizing:current",
    "Extracting Frames:todo", "Publishing:todo", "Done:todo"]);
  assert.equal(view.headline, "Summarizing… (30%)");
  assert.equal(view.tone, "busy");
  assert.equal(view.finished, false);
  assert.equal(view.link, null);
  assert.equal(describeJob({ ...base, status: "QUEUED", progress: 0, title: null }).headline, "Queued…");
  assert.equal(describeJob({ ...base, status: "QUEUED", title: null }).title, "YouTube video dQw4w9WgXcQ");
});

test("popup job: COMPLETED shows Done and a View in Notion link with the gateway's URL", () => {
  const url = "https://www.notion.so/Test-Video-0123456789abcdef0123456789abcdef";
  const view = describeJob({ ...base, status: "COMPLETED", progress: 100,
    result: { notionPageUrl: url, framesRequested: 3, framesPublished: 3 } });
  assert.equal(view.headline, "Done");
  assert.equal(view.tone, "ok");
  assert.ok(view.steps.every((s) => s.state === "done"));
  assert.deepEqual(view.link, { href: url, label: "View in Notion" });
  assert.deepEqual(view.details, []);
});

test("popup job: COMPLETED_WITH_WARNINGS shows the link and each warning", () => {
  const view = describeJob({ ...base, status: "COMPLETED_WITH_WARNINGS",
    result: { notionPageUrl: "https://acme.notion.site/P-1", framesRequested: 3, framesPublished: 1 },
    warnings: [{ code: "FRAME_EXTRACTION_FAILED", timestampSec: 461, reason: "TIMESTAMP_OUT_OF_RANGE" },
      { code: "FRAME_UPLOAD_FAILED", timestampSec: null, reason: null }] });
  assert.equal(view.headline, "Done, with warnings");
  assert.equal(view.tone, "warn");
  assert.equal(view.link.href, "https://acme.notion.site/P-1");
  assert.deepEqual(view.details, ["1 of 3 frames were added.", "The frame at 7:41 could not be captured.",
    "A frame could not be uploaded to Notion."]);
});

test("popup job: FAILED is clearly different from processing (failed step, error, no link)", () => {
  const view = describeJob({ ...base, status: "FAILED", lastStage: "TRANSCRIBING",
    error: { code: "VIDEO_TOO_LONG", message: "raw" }, result: null });
  assert.equal(view.headline, "Failed");
  assert.equal(view.tone, "error");
  assert.equal(view.finished, true);
  assert.deepEqual(stepStates(view).slice(0, 3), ["Queued:done", "Transcribing:failed", "Summarizing:todo"]);
  assert.deepEqual(view.details, ["The video is longer than 20 minutes."]);
  assert.equal(view.link, null);
  const other = describeJob({ ...base, status: "FAILED", error: { code: "SOMETHING_NEW", message: "Gateway text." } });
  assert.deepEqual(other.details, ["Gateway text."]);
});

test("popup job: DEAD_LETTERED explains the last cause", () => {
  const view = describeJob({ ...base, status: "DEAD_LETTERED", lastStage: "SUMMARIZING",
    error: { code: "RETRIES_EXHAUSTED", message: "x", lastErrorCode: "GEMINI_DAILY_LIMIT_REACHED" } });
  assert.equal(view.headline, "Failed after retries");
  assert.match(view.details[0], /Last cause: the AI model's daily quota is used up\./);
});

test("popup job: waiting for a retry and gateway trouble are shown as still processing", () => {
  const waiting = describeJob({ ...base, status: "PUBLISHING", lastStage: "PUBLISHING", progress: 85,
    retry: { reason: "NOTION_RATE_LIMITED", attempt: 1, maxAttempts: 5 } });
  assert.equal(waiting.tone, "wait");
  assert.equal(waiting.headline, "Publishing… (85%)");
  assert.deepEqual(waiting.details, ["Waiting to retry: Notion is limiting requests (attempt 1 of 5). Still processing."]);
  assert.equal(waiting.finished, false);
  const offline = describeJob({ ...base, status: "TRANSCRIBING", connection: "unreachable" });
  assert.deepEqual(offline.details, ["Cannot reach the gateway; still trying."]);
});

test("popup job: NOT_FOUND and unsafe result links", () => {
  assert.equal(describeJob({ ...base, status: "NOT_FOUND" }).headline, "Job not found");
  for (const bad of ["javascript:alert(1)", "http://www.notion.so/x", "https://notion.so.evil.example/x", "", null]) {
    assert.equal(safeNotionUrl(bad), null, String(bad));
    assert.equal(describeJob({ ...base, status: "COMPLETED", result: { notionPageUrl: bad } }).link, null);
  }
  assert.equal(safeNotionUrl("https://www.notion.com/P-1"), "https://www.notion.com/P-1");
});

test("popup: reason codes become readable text", () => {
  assert.equal(reasonText("GEMINI_RATE_LIMITED"), "the AI model is limiting requests");
  assert.equal(reasonText("YOUTUBE_UNREACHABLE"), "YouTube could not be reached");
  assert.equal(reasonText("NOTION_UNAVAILABLE"), "Notion had a temporary problem");
  assert.equal(reasonText("SOMETHING_ELSE"), "SOMETHING_ELSE");
  assert.equal(reasonText(undefined), "a temporary problem");
});

// ------------------------------------------------------------------ ADR-21: provider 5xx wording
test("popup: Gemini 503 / UNAVAILABLE after exhausted retries is clear and suggests a later retry", () => {
  const view = describeJob({ ...base, status: "DEAD_LETTERED", lastStage: "SUMMARIZING",
    error: { code: "RETRIES_EXHAUSTED", message: "raw backend text", lastErrorCode: "GEMINI_HTTP_503",
      lastProviderCode: "UNAVAILABLE" } });
  assert.equal(view.headline, "Failed after retries");
  assert.deepEqual(view.details, ["Processing kept failing and was stopped after several attempts. "
    + "Last cause: the AI model is temporarily unavailable (overloaded). Please try again later."]);
  assert.ok(!view.details[0].includes("GEMINI_HTTP_503") && !view.details[0].includes("{"));
  assert.deepEqual(stepStates(view).slice(0, 3), ["Queued:done", "Transcribing:done", "Summarizing:failed"]);
});

test("popup: Gemini 504, other 5xx, provider code only, and non-5xx causes", () => {
  const dead = (lastErrorCode, lastProviderCode = null) => describeJob({ ...base, status: "DEAD_LETTERED",
    error: { code: "RETRIES_EXHAUSTED", lastErrorCode, lastProviderCode } }).details[0];
  assert.match(dead("GEMINI_HTTP_504"), /Last cause: the AI model took too long to respond\. Please try again later\.$/);
  assert.match(dead("GEMINI_HTTP_500"), /the AI model had a temporary server problem\. Please try again later\.$/);
  assert.match(dead("NOTION_HTTP_503"), /Last cause: Notion is temporarily unavailable \(overloaded\)/);
  assert.match(dead("GEMINI_UNEXPECTED_RESPONSE", "UNAVAILABLE"), /the AI model is temporarily unavailable/);
  assert.match(dead("GEMINI_DAILY_LIMIT_REACHED"), /daily quota is used up\.$/);           // no "try again" hint
  assert.match(dead("GEMINI_INVALID_OUTPUT"), /Last cause: GEMINI_INVALID_OUTPUT\.$/);       // unchanged fallback
});

test("popup: a job waiting after a Gemini 503 is shown as still processing with the reason", () => {
  const view = describeJob({ ...base, status: "SUMMARIZING", lastStage: "SUMMARIZING",
    retry: { reason: "GEMINI_HTTP_503", attempt: 2, maxAttempts: 5 } });
  assert.equal(view.tone, "wait");
  assert.equal(view.finished, false);
  assert.deepEqual(view.details,
    ["Waiting to retry: the AI model is temporarily unavailable (overloaded) (attempt 2 of 5). Still processing."]);
});

test("popup: reasonText maps provider HTTP codes", () => {
  assert.equal(reasonText("GEMINI_HTTP_503"), "the AI model is temporarily unavailable (overloaded)");
  assert.equal(reasonText("GEMINI_HTTP_504"), "the AI model took too long to respond");
  assert.equal(reasonText("YOUTUBE_HTTP_502"), "YouTube had a temporary server problem");
  assert.equal(reasonText("GEMINI_HTTP_400"), "GEMINI_HTTP_400");
  assert.equal(reasonText("GEMINI_SOMETHING", "DEADLINE_EXCEEDED"), "the AI model took too long to respond");
});
