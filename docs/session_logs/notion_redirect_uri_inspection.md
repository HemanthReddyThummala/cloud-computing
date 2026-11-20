# Session log — Notion OAuth redirect URI inspection (read-only)

- **Date:** 2026-11-20 21:52 (host clock)
- **Branch:** `phase-2`; HEAD `1aa6863`; 0 staged; 29 status entries (uncommitted Phase B work,
  untouched).
- **Context:** the user created the Notion public integration "Video Notes Agent". Question: which exact
  OAuth redirect URI must the Chrome extension use?
- **Nature:** inspection only. No code, configuration, `.env` or Notion settings changed; no secrets
  read out (only "empty / set" for `.env` variables). This log is the only file written. No Git
  operations.

Labels: **[REPO]** repository · **[DERIVED]** computed from repository data · **[DOC]** official Chrome
documentation read today · **[NV]** not verified.

## 1. Files inspected
- `extension/` — the whole directory contains only `manifest.json`.
- `docs/decisions.md` ADR-07 (Notion OAuth) and ADR-08 (extension identity).
- `.env.example`, the local `.env` (Notion variables: presence only), `docker-compose.yml`,
  `backend/notesagent/common/config.py`.
- Repository-wide search for `redirect_uri`, `NOTION_REDIRECT`, `chromiumapp`, `launchWebAuthFlow`,
  `getRedirectURL`, `oauth` (excluding session logs and the handover report).
- `https://developer.chrome.com/docs/extensions/reference/api/identity` (via a web-fetch helper).

## 2. Findings

### 2.1 Extension ID
- **[REPO]** `manifest.json` fields: `description`, `key`, `manifest_version` (3), `name`, `version`.
  No `permissions`, no `host_permissions`, no `background`.
- **[DERIVED]** Chrome derives an extension's ID from the SHA-256 of the DER public key in `key`: the
  first 32 hex digits, each mapped `0-f` → `a-p`. Computed from the manifest's `key` (294-byte DER):
  **`kkkeigfecmfhmainhbkcpdhjnloifbdf`**, identical to the ID recorded in ADR-08. The `key` is a public
  key; the private key (`.secrets/extension-key.pem`) was not touched.
- **[NV]** Not yet confirmed by loading the unpacked extension in Chrome and reading the ID shown on
  `chrome://extensions`.

### 2.2 Existing OAuth implementation / callback
- **[REPO]** None:
  - no background/service-worker script, no content script, no popup;
  - no `identity` permission;
  - no `chrome.identity` usage;
  - no gateway OAuth endpoint (code exchange/refresh), and no Notion settings in `config.py` or
    Compose except `NOTION_RPS_LIMIT`.
- **[REPO]** `NOTION_REDIRECT_URI` appears only in `.env.example` ("set only after redirect-URI
  verification"). In the local `.env`, `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`,
  `NOTION_REDIRECT_URI` and `NOTION_DEFAULT_PARENT_ID` are all **empty** (values not shown).

### 2.3 What Chrome's Identity API documents
- **[DOC]** `getRedirectURL(path?)` "Generates a redirect URL to be used in `launchWebAuthFlow`". "The
  generated URLs match the pattern `https://<app-id>.chromiumapp.org/*`", and `path` is "appended to the
  end of the generated URL".
- **[DOC]** The required manifest permission is **`identity`**.
- **[DOC]** `launchWebAuthFlow`: "When the provider redirects to a URL matching the pattern
  `https://<app-id>.chromiumapp.org/*`, the window will close", and the final URL (with `code`/`state`)
  is returned to the extension.
- **[DOC]** The page does **not** give an example value, does not say whether a trailing slash is
  added or how `path` is joined, does not say that `<app-id>` is the manifest-key ID, and says nothing
  about registering the URL with an OAuth provider.

### 2.4 Comparison with the ADR-07 candidate
- ADR-07 candidate: `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion`.
- The host part matches the derived extension ID and the documented pattern `https://<app-id>.chromiumapp.org/*`.
- **Expected** value of `chrome.identity.getRedirectURL("notion")`:
  `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion` — **[NV]**. The exact string
  (slash/joining) is not documented and must be read from Chrome.
- **Still unknown [NV]:** whether Notion accepts a `chromiumapp.org` redirect URI. Notion's
  authorization docs (read in an earlier session) do not state which forms are allowed.
- An OAuth redirect URI must match **character for character** between the Notion integration
  settings, the authorize URL and the gateway's token exchange (`redirect_uri` is required in the
  exchange when it was sent in the authorize request).

## 3. Status
**Not established.** The candidate is plausible and consistent with the derived ID and the documented
pattern, but no code exists that can produce or receive the redirect. The exact string has to be read
from Chrome, and Notion's acceptance has to be tested. Implementation (at least a minimal extension
change) is needed before the URI can be confirmed.

## 4. Recommended next action (needs approval; not done here)
1. **Minimal extension change (Phase C groundwork):** add `"permissions": ["identity"]` and a background
   service worker to `extension/manifest.json`, load the unpacked extension in Chrome, and confirm on
   `chrome://extensions` that the ID is `kkkeigfecmfhmainhbkcpdhjnloifbdf`. In the service-worker
   DevTools console, run `chrome.identity.getRedirectURL("notion")` and copy the **exact** string.
2. The user registers exactly that string as a redirect URI in the Notion integration "Video Notes
   Agent", and puts it (with the client ID/secret) only into the local `.env` as `NOTION_REDIRECT_URI`.
3. Verify Notion's acceptance with a real authorize round trip:
   `launchWebAuthFlow` on `https://api.notion.com/v1/oauth/authorize?owner=user&client_id=…&redirect_uri=<exact>&response_type=code&state=…`
   must return to the `chromiumapp.org` URL with `code` and `state`. If Notion rejects the URI, fall
   back to a gateway-hosted callback (ADR-07 "Mode B" — also unverified), which needs an approved
   design decision.

## 5. Unresolved
- The exact `getRedirectURL` output (expected, not observed).
- Whether Notion accepts `chromiumapp.org` redirect URIs.
- The ID shown by Chrome for the loaded unpacked extension (derived, not observed).

## 6. Git
No staging, commit or push. HEAD `1aa6863`; 0 staged.
