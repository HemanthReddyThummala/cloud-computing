# Phase C Part 2 — Chrome extension Notion OAuth sign-in

Date: 2026-11-20. Scope: extension sign-in and sign-out, sealed-token storage, popup connection status,
and job submission with the sealed token.

Not done (by instruction):
- no deployment;
- no real Notion sign-in;
- no `.env` change;
- no gateway change;
- no Git operations;
- no content script and no job-progress UI.

## 1. Inspection (before changes)

| Source | Finding |
|---|---|
| `extension/` | Only `manifest.json` (permissions `identity`; module-less service worker; `key`), `background.js` (redirect-URI probe helper) and `README.md`. No popup, no storage, no network code |
| `PROJECT_SPEC.pdf` §2 / §5 / §8 | **Background script:** a one-time Notion OAuth that stores the token, so the user is not prompted on every job; it submits jobs and relays status. **Popup:** human-readable progress. **`POST /jobs`:** `notion_token` is supplied by the extension on every submission. **Packaging:** unpacked, no build step |
| ADR-07 / 08 / 18, `docs/api.md`, Phase C Part 1 log | **Redirect:** Mode A, `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion`. **Endpoints:** `POST /oauth/notion/authorize` (no body) → `{authorization_url, state, redirect_uri, expires_in}`; `POST /oauth/notion/callback` with `{state, code}` or `{state, error}` → `{notion_token: "nva1.…", token_format: "sealed-v1", workspace_*, bot_id}`. **Errors:** the `{error: {code, message}}` shape. **`/jobs`:** requires `Content-Type: application/json`; a tampered sealed token → 400 `INVALID_NOTION_TOKEN` |
| `docs/session_logs/extension_redirect_uri_probe.md` | `getRedirectURL("notion")` was read in real Chrome and matches the URI above |
| `docker-compose.yml` / `.env.example` | The gateway is published on `127.0.0.1:${GATEWAY_PORT:-8000}`; `PUBLIC_BASE_URL=http://localhost:8000`. `curl http://localhost:8000/livez` → 200 |
| `.gitignore` | `*token*.json` is ignored, so no file was given such a name |
| Tooling | Node v24.19.0 on the host → `node:test` with no dependencies |

## 2. Design decisions (ADR-19)

1. **Background only.**
   - Only the service worker calls the gateway and reads the stored token.
   - The popup uses runtime messages.
   - Messages from other senders (`sender.id` ≠ our ID) or with unknown types are ignored.
2. **ES-module service worker** (`"type": "module"`), so the same files run in Chrome and in Node
   tests. No build step.
3. **Local checks before trusting a redirect:**
   - the gateway's `redirect_uri` must equal `getRedirectURL("notion")` (else `REDIRECT_MISMATCH`, no
     window opens);
   - `authorization_url` must be https;
   - the returned URL must be our redirect URI;
   - its `state` must equal the issued state (else `STATE_MISMATCH`; the code is never forwarded).
4. **Storage.**
   - `chrome.storage.local` `notionAuth` holds only the sealed token and non-secret metadata.
   - `chrome.storage.session` `notionAuthError` holds only `{code, message}`.
   - A stored value that is not `nva1.…` counts as "not connected".
5. **Repeated attempts.**
   - Concurrent clicks share one attempt.
   - A new attempt clears the old error.
   - A failed re-sign-in keeps the existing connection.
   - Sign-out invalidates an attempt that is still running.
6. **Job submission.**
   - `job:submit` sends the sealed token to `POST /jobs`.
   - `INVALID_NOTION_TOKEN` clears the connection → `NOTION_RECONNECT_REQUIRED`.
7. **No CORS.** The service worker has a host permission for the gateway, and no other context calls
   the gateway.
8. **Timeouts.** 30 s per gateway request (the callback can take about 5 s for the limiter plus 15 s for
   the exchange). Network failure → `GATEWAY_UNREACHABLE`; no answer → `GATEWAY_TIMEOUT`.

## 3. Files changed

| File | Change / why |
|---|---|
| `extension/manifest.json` | + `storage` permission (token storage); + `host_permissions: ["http://localhost:8000/*"]` (gateway calls from the service worker); background `type: module`; `action.default_popup`. `key` unchanged |
| `extension/background.js` | Rewritten as a module: builds the auth service, routes runtime messages, keeps `showNotionRedirectUri()` (logged on install/startup as before) |
| `extension/config.js` (new) | Gateway base URL, redirect path, timeout |
| `extension/lib/gateway.js` (new) | `callGateway`: JSON in/out; maps network/timeout/error-shape/invalid responses to `ExtensionError` codes; keeps Retry-After |
| `extension/lib/auth.js` (new) | sign-in flow, status, sign-out, `submitJob`, error messages, message handler |
| `extension/lib/popup-view.js` (new) | Pure view model for the popup (testable) |
| `extension/popup.html/.js/.css` (new) | Status line, detail/error line, Connect/Disconnect button; refreshes on `storage.onChanged`; light/dark |
| `extension/package.json` (new) | `"type": "module"` + `npm test` for Node only; Chrome ignores it |
| `extension/tests/support.js`, `auth.test.js`, `manifest_and_popup.test.js` (new) | Fake chrome API, local fake gateway (follows `docs/api.md`), 39 tests |
| `extension/tests/live-gateway.check.js` (new) | Optional cross-check against a real gateway configured with the fake Notion token endpoint |
| `extension/README.md` | Rewritten: loading, connecting, what is stored, messages, tests |
| `docs/decisions.md` | ADR-19 |
| `docs/api.md` | Notes that the caller is the background service worker (no CORS) |
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | Phase C Part 2 row, ADR-19, R1/R2 status, next task |

No backend file was changed in this task. The `backend/` entries in `git status` are the uncommitted
Phase C Part 1 work.

## 4. How the flow works

1. The user clicks **Connect Notion** in the popup, which sends `auth:signIn` to the background.
2. The background calls `POST /oauth/notion/authorize` and checks the response and the redirect URI.
3. `chrome.identity.launchWebAuthFlow({url, interactive: true})` opens Notion. The popup closes, which
   is normal.
4. The user allows access. Notion redirects to `https://<id>.chromiumapp.org/notion?code=…&state=…`,
   and Chrome hands that URL back.
5. The background checks the URL and the state, then calls `POST /oauth/notion/callback` with
   `{state, code}`.
   - On a denial it sends `{state, error}`.
   - If the window is closed, Chrome rejects with "The user did not approve access." →
     `SIGN_IN_CANCELLED`, and the gateway is not called; the unused state expires there.
6. The gateway exchanges the code and returns the sealed token. The background stores it with the
   workspace name.
7. When the popup is reopened, it shows "Connected to Notion: <workspace>" or the readable error.
8. A later `job:submit` sends `{youtube_url, notion_token: <sealed>}` to `POST /jobs`.

## 5. Tests and results

### `cd extension && npm test` (Node 24.19.0) — **39 passed, 0 failed** (6 suites, 0.92 s)
- **Success:**
  - call order authorize → window → callback;
  - callback body `{state, code}` with the JSON content type;
  - stored keys are exactly the six allowed fields;
  - no code or state in storage;
  - status without the token;
  - logs without code, state or token.
- **Cancellation and OAuth errors:**
  - window closed → `SIGN_IN_CANCELLED` (no callback, nothing stored, error recorded);
  - other launch failures → `SIGN_IN_FAILED`;
  - denial forwarded → `OAUTH_ACCESS_DENIED`;
  - foreign state → `STATE_MISMATCH` (code not forwarded);
  - denial without state handled locally;
  - no code → `OAUTH_CODE_MISSING`;
  - expired state → `OAUTH_STATE_INVALID`;
  - exchange failure; rate limit with Retry-After 7;
  - redirect mismatch before any window; returned URL not ours.
- **Gateway problems:**
  - down → `GATEWAY_UNREACHABLE`;
  - hanging → `GATEWAY_TIMEOUT`;
  - 6 invalid-response cases (HTML, missing state, non-https URL, raw unsealed token, array body,
    500 without the error shape) → `GATEWAY_INVALID_RESPONSE`, nothing stored;
  - `OAUTH_NOT_CONFIGURED` passed through.
- **Repeated attempts:**
  - concurrent clicks → one authorize and one window;
  - retry after a failure;
  - a failed re-sign-in keeps the connection;
  - sign-out, including during a running attempt.
- **Jobs:**
  - the sealed token is sent; `notion_parent_id` is passed through;
  - not signed in → no request;
  - `INVALID_NOTION_TOKEN` → reconnect required and the connection cleared;
  - other errors keep the connection;
  - a tampered stored value is ignored.
- **Message handler:**
  - own messages work and responses never contain the token;
  - errors become `{ok: false, error}`;
  - foreign senders and unknown types are ignored.
- **Manifest and popup:**
  - exact permissions and host permissions;
  - the host permission covers `GATEWAY_BASE_URL`;
  - `key` unchanged; all referenced files exist;
  - popup view states.
- `node --check` passes for every extension file, including `popup.js` and `background.js`, which need
  Chrome globals and are not imported by the tests.

### Cross-check against the REAL gateway (fake Notion) — **8/8 PASS**
- **Setup:**
  - throwaway `tests` container (`docker compose --profile test run --rm --no-deps`, published only on
    `127.0.0.1:18080`);
  - isolated test infrastructure (`notes_test` DB, vhost `notes_test`, Redis db 15 flushed);
  - fresh random encryption key; fake client ID and secret;
  - `NOTION_API_BASE_URL` → `tests.support.fake_notion` inside the container.
  - The script was a scratchpad file mounted read-only; nothing was added to the repository except
    `live-gateway.check.js`.
- **Harness note:** the fake Notion base is http, so the harness upgrades only the `authorization_url`
  scheme. Nothing loads that URL.
- **Results** (`GATEWAY=http://127.0.0.1:18080 node tests/live-gateway.check.js`, exit 0):
  - a real sealed token is stored and the raw token never reaches the extension;
  - `POST /jobs` with the sealed token → 202 QUEUED;
  - a tampered token → `NOTION_RECONNECT_REQUIRED`;
  - denial → `OAUTH_ACCESS_DENIED`;
  - rejected code → `OAUTH_EXCHANGE_FAILED`;
  - missing code → `OAUTH_CODE_MISSING`;
  - a replayed state is refused locally (`STATE_MISMATCH`), and the gateway rejects it directly
    (`OAUTH_STATE_INVALID`);
  - the extension logs carry no secrets.
- **Gateway log:** 22 lines with outcome messages only; **0 hits** for codes, fake tokens, `nva1.` or the
  fake secret. The container was stopped and removed (`--rm`).
- **No gateway integration bug found**, so the gateway was not changed.

### Backend suite
Not re-run, because no backend file changed. The last full run (Phase C Part 1): 861 passed.

## 6. Manual steps still required (user)

1. **Approval needed:** deploy the current backend image. The running dev stack is still the Phase B
   Part 1 image, without OAuth.
2. Load or reload the unpacked extension (`extension/README.md`) and check that the ID is
   `kkkeigfecmfhmainhbkcpdhjnloifbdf`.
3. Click **Connect Notion**, share the "Video Notes" page and allow access. This is the first real
   sign-in, and it also confirms that Notion accepts the `chromiumapp.org` redirect.
4. If something fails, the popup shows the error code and message. The service-worker console shows
   only outcome codes.

## 7. Unresolved issues / limits

- **Not verified in real Chrome by me:** loading the module service worker, the popup, and
  `launchWebAuthFlow` against real Notion (needs the user and approval). Chrome APIs used:
  promise-based `identity.launchWebAuthFlow`, `storage.session`, `AbortSignal.timeout`, all available
  in current Chrome.
- **Cancellation detection** relies on Chrome's rejection text ("did not approve"). Other texts map to
  `SIGN_IN_FAILED`, which is still a clean, retryable error.
- **Service-worker lifetime** during a very long sign-in is not proven [NV]. If Chrome stops the worker,
  the user clicks Connect again.
- **Sign-out is local**; Notion-side revocation is done in Notion's settings.
- **Gateway URL** is fixed at `http://localhost:8000` (config plus manifest). Changing the port needs
  both edits.
- **Not built yet:** the content script, status polling and the progress popup (Phase C Part 3).

## 8. Next recommended task

1. With approval: deploy the current image and do the first real Notion sign-in through the extension.
2. **Phase C Part 3:**
   - content-script "Take Notes" button (YouTube SPA navigation) → `job:submit`;
   - background status polling (`GET /jobs/{id}`, `/results`);
   - progress popup (Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done,
     "View in Notion", warnings and failures).

   Then the first real end-to-end job.
