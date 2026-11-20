// MV3 background service worker (ES module). Owns the Notion connection (ADR-18/19) and the job
// tracking (ADR-20): it is the only part of the extension that talks to the gateway and the only place
// the sealed token is read. Other extension contexts talk to it via chrome.runtime messages:
//   {type: "auth:status"} | {type: "auth:signIn"} | {type: "auth:signOut"}
//   {type: "job:submit", youtubeUrl}          (from the YouTube content script)
// The popup reads tracked jobs from chrome.storage.local["jobs"] (display data only).

import { GATEWAY_BASE_URL, NOTION_REDIRECT_PATH, REQUEST_TIMEOUT_MS } from "./config.js";
import { createAuthService, createMessageHandler } from "./lib/auth.js";
import { createJobTracker } from "./lib/jobs.js";

const fetchImpl = (...args) => fetch(...args);
const auth = createAuthService({
  chrome,
  fetch: fetchImpl,
  baseUrl: GATEWAY_BASE_URL,
  redirectPath: NOTION_REDIRECT_PATH,
  timeoutMs: REQUEST_TIMEOUT_MS,
});
const jobs = createJobTracker({ chrome, fetch: fetchImpl, baseUrl: GATEWAY_BASE_URL, auth });

// "job:submit" goes through the tracker (duplicate prevention, idempotency key, polling).
const handle = createMessageHandler({ ...auth, submitJob: (args) => jobs.submit(args) }, chrome.runtime.id);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const pending = handle(message, sender);
  if (pending === null) return false;
  pending.then(sendResponse);
  return true; // respond asynchronously
});

// Any event that starts this worker (popup message, content-script click, browser start) runs this
// top-level code, so unfinished jobs are polled again after the worker was stopped.
jobs.resume();

// Diagnostics kept from the redirect-URI probe (ADR-07): callable from the service-worker console.
// chrome.identity.getRedirectURL() only builds a string locally.
function showNotionRedirectUri() {
  const uri = chrome.identity.getRedirectURL(NOTION_REDIRECT_PATH);
  console.log(`[Video Notes Agent] extension id: ${chrome.runtime.id}`);
  console.log(`[Video Notes Agent] Notion redirect URI: ${uri}`);
  return uri;
}

globalThis.showNotionRedirectUri = showNotionRedirectUri;
chrome.runtime.onInstalled.addListener(showNotionRedirectUri);
chrome.runtime.onStartup.addListener(showNotionRedirectUri);
