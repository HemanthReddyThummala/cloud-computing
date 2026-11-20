// Minimal MV3 service worker — ONLY for verifying the Notion OAuth redirect URI (ADR-07).
// It starts no OAuth flow and makes no network requests: chrome.identity.getRedirectURL() only
// builds a string locally (https://<extension-id>.chromiumapp.org/<path>).

const NOTION_REDIRECT_PATH = "notion";

function showNotionRedirectUri() {
  const uri = chrome.identity.getRedirectURL(NOTION_REDIRECT_PATH);
  console.log(`[Video Notes Agent] extension id: ${chrome.runtime.id}`);
  console.log(`[Video Notes Agent] Notion redirect URI: ${uri}`);
  return uri;
}

// Callable from the service-worker DevTools console: showNotionRedirectUri()
globalThis.showNotionRedirectUri = showNotionRedirectUri;

// Also log once whenever the worker is installed/reloaded or the browser starts.
chrome.runtime.onInstalled.addListener(showNotionRedirectUri);
chrome.runtime.onStartup.addListener(showNotionRedirectUri);
