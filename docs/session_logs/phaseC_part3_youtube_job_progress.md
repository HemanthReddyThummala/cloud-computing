# Phase C Part 3 — YouTube Take Notes button, job submission, status polling, progress UI

Date: 2026-11-20. Scope: content script, background job submission and polling, popup progress.

Not done (by instruction):
- no deployment;
- no real Notion sign-in;
- no real video job;
- no `.env` change;
- no OAuth change;
- no backend change;
- no Git operations.

## 1. Inspection (before coding)

| Source | Finding used |
|---|---|
| Spec §2 | **Content script:** a "Take Notes" button in the **player toolbar** on watch pages; it sends the video to the background and must keep working when navigating between videos. **Background:** submits jobs and relays status. **Popup:** Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done, a one-click "View in Notion", clear failures and "completed with warnings", never raw JSON |
| Spec §9 | A "batch" means clicking Take Notes on several videos in a row, each its own job; no batch UI |
| `docs/api.md`, `common/states.py` | **`GET /jobs/{id}`** returns `status`, `progress`, `video_title`, `duration_sec`, `warnings`, `error`, `result`, `retry` (enough on its own; `/results` is not needed). **Statuses:** QUEUED, TRANSCRIBING, SUMMARIZING, EXTRACTING_FRAMES, PUBLISHING, COMPLETED, COMPLETED_WITH_WARNINGS, FAILED, DEAD_LETTERED. **Progress:** 0/10/30/55/85/100. **Unknown id:** 404 `JOB_NOT_FOUND`. **`result`:** only on COMPLETED*. **`error`:** only on FAILED/DEAD_LETTERED (`RETRIES_EXHAUSTED` + `last_error_code`, `DEAD_LETTERED_BY_BROKER`). **Warning codes:** `FRAME_EXTRACTION_FAILED`, `FRAME_UPLOAD_FAILED` (with `timestamp_sec`) |
| `POST /jobs` | Optional `Idempotency-Key` (8–128 chars `A-Za-z0-9-_:.`, TTL 24 h): the same key and body → the original 202 replayed; the same key with another body → 422 `IDEMPOTENCY_KEY_REUSED`; 409 `IDEMPOTENCY_CONFLICT` → retry. 503 `BROKER_/DATABASE_UNAVAILABLE` → nothing was created |
| Backend reason codes | Per-provider codes such as `GEMINI_RATE_LIMITED`, `GEMINI_DAILY_LIMIT_REACHED`, `NOTION_UNREACHABLE`, `YOUTUBE_BLOCKED`, `*_COOLDOWN_ACTIVE`, `*_TLS_ERROR`, many generated as `{provider}_…`. The popup maps them by provider prefix and suffix |
| ADR-18/19, Part 2 log, `extension/` | `lib/auth.js` owns the sealed token (`submitJob`), messages `auth:*`/`job:submit`, sender check, permissions `identity`, `storage`, host `http://localhost:8000/*` |

## 2. What was implemented

1. **Content script** (`content.js` + `content.css`, classic script, `https://www.youtube.com/*`):
   - one `#vna-take-notes` button, prepended to `#movie_player .ytp-right-controls`, only on `/watch`
     pages with a valid 11-character `v`;
   - kept in sync with YouTube's in-page navigation by `yt-navigate-finish`, `popstate` and a 1 s
     watchdog (late player build, rebuilt toolbar, missed events);
   - removed off watch pages; never duplicated;
   - per-video state in the tab: Take Notes / Submitting… / Notes requested ✓ / Notes in progress /
     Take Notes (retry) with the reason in the tooltip;
   - sends only `{type: "job:submit", youtubeUrl: "https://www.youtube.com/watch?v=<id>"}`.
2. **Background tracker** (`lib/jobs.js`; wired in `background.js`):
   - **Before any request:** URL check (`NOT_A_VIDEO`) and Notion check (`NOT_SIGNED_IN`).
   - **Duplicate prevention:**
     - an active job for the same video is returned as `alreadyTracking`;
     - concurrent clicks share one request;
     - an `Idempotency-Key` `vna:<videoId>:<uuid>` is kept after an uncertain outcome and reused on
       the next click;
     - one automatic retry with a fresh key on `IDEMPOTENCY_KEY_REUSED`.
   - **Polling:** a single sequential loop over `GET /jobs/{id}`:
     - 3 s while running; 10 s while `retry` is set;
     - exponential backoff capped at 30 s on errors;
     - stops on terminal states or 404 (`NOT_FOUND`).
   - **Storage:** jobs persisted in `chrome.storage.local["jobs"]` (display data only, ≤ 10), with
     serialised writes.
   - **Restart recovery:** `resume()` runs on every service-worker start.
3. **Popup** (`popup.html/.js/.css`, `lib/popup-view.js`):
   - the Notion section is unchanged;
   - a new "Notes" list shows, per job:
     - the six-step sequence with done / current / failed marks and %;
     - "Waiting to retry: <reason> (attempt n of m). Still processing.";
     - "Cannot reach the gateway; still trying.";
     - **Done** + View in Notion;
     - **Done, with warnings** + "k of n frames were added." + each warning;
     - **Failed** / **Failed after retries** in red, with the failed stage and a readable cause;
     - **Job not found**.
   - The link is shown only for an https URL on notion.so / notion.site / notion.com.
   - Built with `createElement`/`textContent` (no `innerHTML`); re-rendered on `storage.onChanged`.

## 3. Files changed and why

| File | Change / why |
|---|---|
| `extension/content.js`, `content.css` (new) | The Take Notes button (spec §2 content script) |
| `extension/lib/jobs.js` (new) | Submission, duplicate prevention, polling, restart recovery |
| `extension/background.js` | Creates the tracker; `job:submit` goes through `jobs.submit` (the existing `createMessageHandler` gets a service object whose `submitJob` is the tracker); calls `jobs.resume()` at start |
| `extension/lib/auth.js` | **Only job-related additions:** `submitJob` accepts `idempotencyKey` and sends it as a header; friendly messages for job codes (`NOT_A_VIDEO`, `INVALID_YOUTUBE_URL`, …). The sign-in flow, storage and checks are unchanged |
| `extension/lib/gateway.js` | `callGateway` accepts extra `headers` (needed for the Idempotency-Key) |
| `extension/lib/popup-view.js` | `describeJob`, `reasonText`, `safeNotionUrl`, `STEPS` |
| `extension/popup.html`, `popup.js`, `popup.css` | Jobs list and styles |
| `extension/manifest.json` | `content_scripts` for `https://www.youtube.com/*`. Permissions and `key` unchanged |
| `extension/tests/fake-dom.js` (new) | Minimal DOM for the content-script tests |
| `extension/tests/content.test.js`, `jobs.test.js` (new) | 15 + 26 tests |
| `extension/tests/support.js` | Fake gateway: `GET /jobs/{id}` scripts, unique job ids, Idempotency-Key replay/422, optional delay; `statusBody`, `waitFor` |
| `extension/tests/manifest_and_popup.test.js` | Content-script manifest test; 7 popup job-view tests |
| `extension/tests/live-gateway.check.js` | 2 tracker checks against the real gateway |
| `extension/README.md`, `docs/api.md`, `docs/decisions.md` (ADR-20), `docs/PROJECT_STATUS_AND_HANDOVER.md` | Documentation |

## 4. Important design decisions (ADR-20)

- **No new permissions.**
  - Content scripts declared for `https://www.youtube.com/*` need no host permission to run.
  - Restart recovery uses "resume on every worker start", not `alarms`. Each poll writes
    `chrome.storage`, and extension API activity keeps the worker alive while jobs run. If Chrome still
    stops it, the next event (opening the popup, clicking Take Notes, browser start) resumes polling.
- **No backend change.** `GET /jobs/{id}` already has everything the popup needs, so `/results` is not
  used.
- **The popup reads `jobs` from storage directly** (display data only). No new message type was
  needed, and the sealed token stays readable only by `lib/auth.js` in the service worker.
- **Content-script state is per tab.** After "Notes requested ✓", the same video can be submitted again
  only after a page reload. The background also refuses a duplicate while that video's job is active.
- **`m.youtube.com`** is not matched (desktop Chrome extension; the spec says "player toolbar").

## 5. Tests and commands (exact results)

| Command | Result |
|---|---|
| `cd extension && node --test tests/content.test.js tests/jobs.test.js` (first run) | 40 passed, 1 failed. The failure was a test artifact: an object from the `vm` sandbox has another realm's prototype, so `deepStrictEqual` failed. The test now compares a JSON copy; no product change |
| `cd extension && npm test` | **89 passed, 0 failed** (9 suites, ≈ 1.9 s). 39 existing + 50 new: content 15, jobs 26, manifest/popup 9 |
| `npm test` × 5 consecutive (flakiness check; several tests use real timers) | **89/89 every run** |
| `node --check` on every extension file; manifest JSON parse | all OK |
| Real-gateway cross-check: throwaway `tests` container (`docker compose --profile test run --rm --no-deps -p 127.0.0.1:18080:8000`, isolated test infrastructure, fake Notion token endpoint, random key, fake client credentials), then `GATEWAY=http://127.0.0.1:18080 node tests/live-gateway.check.js` | **10/10 PASS**, exit 0: the 8 Part 2 checks, plus the tracker submitting with an Idempotency-Key (same key → same job id from the real gateway; real `GET /jobs/{id}` parsed: QUEUED, progress 0, no token in stored jobs) and an unknown id → real 404 → `NOT_FOUND` |
| Gateway log of that container | 27 lines, **0 hits** for codes, fake tokens, `nva1.` or the fake secret. "job accepted" × 3 for 4 submissions: the idempotent replay created no job. Container stopped and removed |

Tests by required area:
- **Button creation and removal:** one button in the toolbar; removed on home, results, shorts and
  invalid ids; restored on return; `stop()`.
- **SPA navigation:** same button updated for the new video; `popstate`; navigation caught by the
  watchdog only; late player; rebuilt toolbar; per-video state kept across navigation; a late response
  does not touch the new video.
- **Duplicate prevention:**
  - DOM: repeated events and ticks; double click; forced second click.
  - Background: active job → `alreadyTracking`; 3 concurrent submits → 1 POST; still prevented after a
    worker restart.
- **Submission with Notion connected:** body is exactly `{youtube_url: canonical, notion_token:
  sealed}` plus the `Idempotency-Key` format; no token in job records, responses or logs; two videos in
  a row → two jobs; a new job after the previous one finished.
- **Notion disconnected:** `NOT_SIGNED_IN` with no request; the button shows "Connect Notion first"
  and stays clickable.
- **Polling and terminal states:**
  - all six stages observed through COMPLETED, then no more GETs;
  - COMPLETED_WITH_WARNINGS (warnings + result);
  - FAILED (error + `lastStage`) and DEAD_LETTERED (`lastErrorCode`);
  - slower polling while `retry` is set;
  - 404 → NOT_FOUND and polling stops;
  - an invalid body is ignored.
- **Restart recovery:** worker A "killed" mid-job, worker B `resume()` → COMPLETED; `resume()` with
  only finished jobs makes no request.
- **Network errors and retry:**
  - poll 502 ×3 → `connection: error`, failures counted, then recovery;
  - gateway down → `unreachable`, retries continue, backoff ≤ cap;
  - a lost POST response → the next click reuses the key → the same job, no duplicate;
  - gateway down at submit → nothing tracked;
  - a validation error drops the key;
  - `IDEMPOTENCY_KEY_REUSED` → one retry with a new key.
- **Single loop:** 10 × `resume()` with 2 jobs and a 20 ms server delay → at most 1 status request in
  flight, bounded GET count.
- **Popup:**
  - stage sequence and %;
  - COMPLETED link (the gateway's URL);
  - COMPLETED_WITH_WARNINGS frame count + warnings;
  - FAILED with failed step and readable cause; unknown code → gateway message;
  - DEAD_LETTERED last cause;
  - waiting retry / unreachable shown as "still processing";
  - unsafe links rejected (`javascript:`, http, look-alike host);
  - reason texts.

Backend suite: not re-run, because no backend file changed. The last run (Phase C Part 1) was 861 passed.

## 6. Issues during the session

- `npm install --save-dev jsdom@25.0.1` (a test-only DOM) was **denied by the tool permission
  system**. It was not retried or worked around. The content script is tested in a small hand-written
  fake DOM (`tests/fake-dom.js`) that implements only the DOM calls `content.js` makes. Consequence: the
  button's real rendering and placement on YouTube are **not** proven by tests and must be checked by
  hand. If wanted, the user can approve adding jsdom later.
- The test artifact described in §5 (cross-realm `deepStrictEqual`).

## 7. Unresolved issues and manual steps

Manual steps (user, with approval):
1. Deploy the current backend image. The dev stack still runs the Phase B Part 1 image, without OAuth
   and the later stages.
2. Reload the unpacked extension (`chrome://extensions`, ID `kkkeigfecmfhmainhbkcpdhjnloifbdf`). Chrome
   will now also show "Read and change your data on www.youtube.com", which comes from the content
   script.
3. Connect Notion (the first real sign-in).
4. Open a short public video, check the button placement and style in the player toolbar, click Take
   Notes, and follow the popup to Done → View in Notion.
5. Navigate to another video without reloading and check that the button follows.

Unresolved / limits:
- **Not verified on real YouTube.** YouTube's DOM (`#movie_player .ytp-right-controls`,
  `yt-navigate-finish`) can change; the 1 s watchdog is the fallback for events, but a changed toolbar
  selector would hide the button.
- **Worker lifetime** while polling relies on Chrome counting extension API calls as activity [NV in
  real Chrome]. A stop is recovered on the next event, not by a timer.
- **The popup lists the last 10 jobs** and has no "clear" action (not required).
- **After "Notes requested ✓"**, re-submitting the same video in the same tab needs a page reload.
- **`m.youtube.com`** and embedded players are not supported.

## 8. Next recommended task

1. With approval: deploy, run the first real sign-in, and run the first real end-to-end job (manual
   steps above).
2. **Phase D:** the README (base URL, default Notion destination, every required env var, loading the
   extension), and the demos (the §6 failure modes, `--scale worker=N`, killing a worker mid-job, burst
   submission without an LLM 429).
