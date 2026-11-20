# Video Notes Agent — Chrome extension

Manifest V3 extension, loaded unpacked; **no build step**. Current state (Phase C Part 3):

- **Notion sign-in** (ADR-18/ADR-19): connect or disconnect Notion from the popup.
- **"Take Notes" button** in the YouTube player toolbar on watch pages. It follows YouTube's in-page
  navigation between videos (ADR-20).
- **Job submission and progress:** the background submits the video with the sealed token, polls the
  job, and the popup shows Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done,
  warnings, failures and a **View in Notion** link.

## Files

| File | Role |
|---|---|
| `manifest.json` | permissions `identity`, `storage`; host permission for the gateway; module service worker; popup; content script on `https://www.youtube.com/*`; `key` (fixes the extension ID `kkkeigfecmfhmainhbkcpdhjnloifbdf`) |
| `config.js` | gateway base URL (`http://localhost:8000`), redirect path `notion`, request timeout |
| `background.js` | service worker: message router, resumes job polling whenever it starts, `showNotionRedirectUri()` console helper |
| `content.js`, `content.css` | YouTube content script: the "Take Notes" button |
| `lib/auth.js` | sign-in / sign-out / status / `submitJob` (the only code that reads the sealed token) |
| `lib/jobs.js` | job submission (duplicate prevention, Idempotency-Key), status polling, restart recovery |
| `lib/gateway.js` | `fetch` wrapper for the gateway's JSON + `{error: {code, message}}` contract |
| `lib/popup-view.js`, `popup.html/.js/.css` | popup: Notion connection + job progress |
| `package.json`, `tests/` | Node test runner only; Chrome ignores them |

## Load the extension

1. Start the backend (`docker compose up -d`). The gateway must answer at `http://localhost:8000`.
2. Open `chrome://extensions` and switch on **Developer mode**.
3. Click **Load unpacked** and select this `extension/` folder.
4. The card must show the ID **`kkkeigfecmfhmainhbkcpdhjnloifbdf`**.
5. After code changes, click the card's reload icon.

If you change `GATEWAY_PORT`, update both `GATEWAY_BASE_URL` in `config.js` and `host_permissions` in
`manifest.json`.

## Connect Notion

1. Click the extension icon, then **Connect Notion**.
2. A Notion window opens and the popup closes; that is normal.
3. Choose the pages to share (include your "Video Notes" page) and allow access.
4. Open the popup again. It shows **Connected to Notion: <workspace>**, or a readable error such as
   "Notion sign-in was cancelled."
5. **Disconnect** forgets the connection in this browser. To revoke the integration itself, remove it
   in Notion (Settings → Connections).

What is stored (`chrome.storage.local`, key `notionAuth`):
- the **sealed** token (`nva1.…`, encrypted by the gateway; useless without the gateway's key);
- workspace name and ID, bot ID, and the connection time.

The raw Notion token, the authorization code and the client secret never reach the extension's
storage. The last sign-in error (`{code, message}` only) is kept in `chrome.storage.session`.

## Take notes on a video

1. Connect Notion (above).
2. Open a YouTube video (`https://www.youtube.com/watch?v=…`). A **Take Notes** button appears at the
   left of the player's right-hand controls (next to the settings gear).
3. Click it. The button shows **Notes requested ✓**; clicking again does nothing for that video.
   - If Notion is not connected, the button turns red; its tooltip says to connect Notion first.
   - On other errors (for example, the gateway is not running), the tooltip gives the reason and the
     button can be clicked again. A retry never creates a second job if the first one was in fact
     created.
4. Open the extension popup to follow progress:
   - **Done**: a **View in Notion** link to the created page.
   - **Done, with warnings**: the link, plus which frames were left out.
   - **Failed** or **Failed after retries**: shown in red with the stage it stopped at and the reason.
   - **Waiting to retry**: still processing (for example, Notion or the AI model is rate-limiting).
5. Several videos in a row are separate jobs; the popup lists the last 10.

The background polls the gateway every 3 s while a job runs. It slows down while the job waits for a
retry or the gateway is unreachable, and stops when the job is finished. If Chrome stops the
background worker, polling continues the next time the worker starts (opening the popup is enough).

Stored for jobs (`chrome.storage.local`, key `jobs`): job ID, video ID and URL, title, status,
progress, warnings, error, the Notion page URL, and poll bookkeeping. No token.

## Messages to the background

Accepted only from this extension's own pages and scripts:

| `type` | Response (`ok: true` …) |
|---|---|
| `auth:status` | `connected`, `workspaceName`, `workspaceId`, `connectedAt`, `signingIn`, `lastError` |
| `auth:signIn` | the status after sign-in. Repeated clicks join the running attempt |
| `auth:signOut` | the status after sign-out |
| `job:submit` (`youtubeUrl`: a youtube.com watch URL) | `jobId`, `status`, `videoId`, `alreadyTracking` |

Errors come back as `{ok: false, error: {code, message}}`. Examples: `SIGN_IN_CANCELLED`,
`GATEWAY_UNREACHABLE`, `OAUTH_STATE_INVALID`, `NOT_SIGNED_IN`, `NOT_A_VIDEO`, `INVALID_YOUTUBE_URL`,
`NOTION_RECONNECT_REQUIRED`. Responses never contain the sealed token.

## Tests

```
cd extension
npm test
```

This needs Node ≥ 20 and has no dependencies. It uses:
- a fake `chrome` API;
- a local fake gateway (real `fetch`) that follows `docs/api.md`;
- for `content.js`, a small fake DOM (`tests/fake-dom.js`). That DOM is not a browser; the button's
  look and placement on the real YouTube page are checked by hand.

The tests never contact Notion or YouTube.

Optional cross-check against a **real** gateway configured with the **fake** Notion token endpoint
(see `docs/session_logs/phaseC_part2_extension_oauth.md` and `phaseC_part3_youtube_job_progress.md`):

```
GATEWAY=http://127.0.0.1:18080 node tests/live-gateway.check.js
```

Never point it at a gateway configured with real Notion credentials.

## Redirect URI helper

In the service-worker console (card → **service worker**), `showNotionRedirectUri()` prints
`chrome.identity.getRedirectURL("notion")`. It must equal `NOTION_REDIRECT_URI` and the URI registered
in the Notion integration: `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion`.
