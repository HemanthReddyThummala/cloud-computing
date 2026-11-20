// Extension configuration (no secrets live in the extension).
// GATEWAY_BASE_URL must be covered by "host_permissions" in manifest.json; if you change the gateway
// port (GATEWAY_PORT in .env), change both places and reload the extension.
export const GATEWAY_BASE_URL = "http://localhost:8000";

// chrome.identity.getRedirectURL(NOTION_REDIRECT_PATH) must equal the gateway's NOTION_REDIRECT_URI and
// the redirect URI registered in the Notion integration (ADR-07 Mode A, ADR-18).
export const NOTION_REDIRECT_PATH = "notion";

// The callback may wait up to ~5 s for a Notion limiter permit plus 15 s for Notion's token endpoint.
export const REQUEST_TIMEOUT_MS = 30000;
