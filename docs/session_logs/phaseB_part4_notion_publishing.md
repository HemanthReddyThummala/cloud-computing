# Session log — Phase B, Part 4: PUBLISHING stage (Notion)

- **Started:** 2026-11-20 22:16 (host clock)
- **Branch:** `phase-2`; HEAD `1aa6863`; 0 staged; 35 status entries at start (uncommitted Phase B
  Parts 1–3, extension probe, inspection logs — preserved). No Git operations are performed.
- **Authoritative spec:** `PROJECT_SPEC.pdf` §3.4 (upload frames, assemble page with headings, bullets,
  captioned images), §5 (`/results` → absolute Notion URL, `frames_requested`, `frames_published`),
  §6 (no duplicate page on Notion after a crash; graceful degradation, text-only page), §7 B (every
  frame paired with an LLM caption).
- **Out of scope:** gateway OAuth, extension functionality.

## 1. Inspection summary
- **Stage contract:** PUBLISHING returns `StageOutput.result`. The runner validates it
  (`validate_publish_result`: absolute https notion.so/notion.site URL, integer counts,
  published ≤ requested) and sets COMPLETED, or COMPLETED_WITH_WARNINGS when there are warnings or
  `frames_published < frames_requested`, in the same transaction as the checkpoint, then acks.
- **Credentials:** `ctx.get_notion_token()` reads the per-job encrypted mailbox (ADR-03).
- **Errors:** `classify_http(Provider.NOTION, status, headers, body, method=…)`:
  - 429/529 → RateLimited (Retry-After);
  - 5xx → transient, with `notion_write_outcome_unknown` for writes;
  - 401/403/404/400 → FAILED.
- **Limiter:** `limiter.acquire(Provider.NOTION, connection=<key>)` (2.5/s per connection, ADR-13).
- **Inputs:** the SUMMARIZING checkpoint (summary, key_points, sections, important_timestamps) and the
  EXTRACTING_FRAMES checkpoint (frames with base64 JPEG, `requested`, `failed`). The job row has
  `notion_parent_id`, `youtube_url`, `video_title`, `video_id`.
- **Notion API facts** (read in earlier sessions; `external_services_credentials_inspection.md`):
  - `POST /v1/pages` (≤ 100 children);
  - `PATCH /v1/blocks/{id}/children` (≤ 100);
  - payload ≤ 500 KB; rich text ≤ 2000 characters;
  - File Upload: `POST /v1/file_uploads` → `POST /v1/file_uploads/{id}/send` (multipart `file`), then
    an image block `{"type": "file_upload", "file_upload": {"id": …}}`;
  - free workspace ≤ 5 MiB per file; attach within 1 h;
  - 429 with Retry-After; a 5xx write may have committed.
- **Configuration:** the worker receives no `NOTION_DEFAULT_PARENT_ID` / `NOTION_VERSION` yet.
  `httpx` is only a test dependency.

## 2. Plan
1. **Dependency:** `httpx==0.28.1` + `httpcore==1.0.9` added to the runtime lock (already pinned for
   tests; their other dependencies are already present).
2. **Settings + Compose (worker):**
   - `NOTION_VERSION` (2026-03-11);
   - `NOTION_DEFAULT_PARENT_ID` (UUID, empty → none);
   - `NOTION_API_BASE_URL` (default `https://api.notion.com/v1`; tests point it to a local fake);
   - `NOTION_BLOCKS_PER_REQUEST` (≤ 100).
3. **`worker/notion.py`:** an async `NotionClient`. It takes a limiter permit (connection key = hash of
   the token, never the token) before EVERY request, sends the Bearer token and Notion-Version, makes no
   retries of its own, and maps errors through `classify_http` (method-aware) or to a transient error
   for transport failures.
4. **`worker/publish.py`:**
   - a pure, deterministic block plan: marker paragraph with the job id first, summary, key points,
     timestamped sections with YouTube links, key frames with LLM captions, and a note about missing
     frames;
   - **reconcile before every write**: find an existing child page of the parent with the same title
     whose first block carries this job's marker, count its blocks, and resume with the remaining part
     of the plan (never a second page);
   - upload only the frames still needed, then create the page / append in ≤ 100-block batches;
   - count the published image blocks from Notion;
   - return the result.
5. **Tests:**
   - unit tests (plan, client with `httpx.MockTransport`, reconciliation, error mapping, permits,
     secrets);
   - integration tests with the real worker against a local **fake Notion HTTP server**: success,
     text-only, partial, 429, uncertain 503 write, crash mid-append, crash after the writes, upload
     failure, 401, missing parent, job-specific parent, no token in logs;
   - full suite.
6. **Docs:** ADR-17, `.env.example`, handover report, this log.

## 3. Files created / modified
| File | Why |
|---|---|
| `backend/notesagent/worker/notion.py` (new) | `NotionClient`: a permit before every request (connection key = token hash), Bearer + Notion-Version headers, no own retries, method-aware `classify_http`, transport errors → `NOTION_UNREACHABLE` (writes marked uncertain); helpers for children listing (paginated), page create/retrieve, append, File Upload (create + send) |
| `backend/notesagent/worker/publish.py` (new) | the stage: deterministic block plan (marker first), rich-text splitting ≤ 2000, captions with time link + caption + reason, text-only and missing-frames notes, reconciliation (`find_existing_page` by title + first-block marker → resume the plan suffix), lazy frame uploads, batching, permanent upload failure → note + `FRAME_UPLOAD_FAILED`, `frames_published` counted from Notion, result + checkpoint |
| `backend/notesagent/worker/stages.py` | the registry adds PUBLISHING (all four stages now production) |
| `backend/notesagent/common/config.py` | `notion_version`, `notion_default_parent_id` (validated, normalised, empty → None), `notion_api_base_url`, `notion_request_timeout_sec`, `notion_blocks_per_request` (≤ 100) |
| `docker-compose.yml` | worker: `NOTION_VERSION`, `NOTION_DEFAULT_PARENT_ID` |
| `.env.example` | explanation for `NOTION_DEFAULT_PARENT_ID` |
| `backend/requirements.in`, `backend/requirements.txt` | `httpx==0.28.1`, `httpcore==1.0.9` (runtime; same pins as the test lock; other dependencies already present) |
| `docs/decisions.md` | ADR-17 |
| `backend/tests/support/fake_notion.py` (new) | fake Notion API (FastAPI): pages, block children (paginated, `plain_text`), file uploads (JPEG required, upload status checked on attach), ≤ 100 children / ≤ 2000 chars validation, request log, token **hashes** only, scenarios (429 once, committed 503 create, failing 503 append, gated append / final listing, upload 400, 401) |
| `backend/tests/support/run_youtube_worker.py` | production PUBLISHING by default (unreachable default Notion URL so tests can never reach the real API); `FAKE_YT_WITH_FIXTURES=1` now means "fixture PUBLISHING" for the earlier stage tests |
| `backend/tests/test_unit_publish.py` (new) | 19 unit tests (in-process via `httpx.ASGITransport`) |
| `backend/tests/test_publish_stage.py` (new) | 9 integration tests (real worker + fake Notion process) |
| `backend/tests/test_worker.py`, `test_summarize_stage.py`, `test_transcribe_stage.py` | jobs without a Notion destination now fail honestly at PUBLISHING with `NOTION_PARENT_MISSING` (instead of `PIPELINE_STAGE_UNAVAILABLE`) |

## 4. Implementation decisions
- **Reconcile before every write instead of trusting a stored page ID:** a mid-stage database write
  would need sub-step checkpoints (deferred earlier). Reading Notion's own state is the only source that
  is right after an uncertain write, and Part 2 (ADR-10) already required reconciliation for
  `notion_write_outcome_unknown`.
- **Marker in the first block + title match:** child-page listing is consistent (unlike search). The
  marker distinguishes jobs with the same video/title.
- **Count-based resume:** blocks are appended strictly in plan order, so the existing blocks are a
  prefix. `frames_published` is counted from the image blocks actually present.
- **Parent check before reading the token:** no Notion request and no credential access when no
  destination exists.
- **`httpx` rather than a transitive client:** already the approved Notion client in the Phase 0 plan
  and pinned for tests.

## 5. Tests
- Test image rebuilt (`docker compose --profile test build tests`) after each change.
- `pytest tests/test_unit_publish.py` → **19 passed** in 1.00 s (first run). Covers:
  - plan determinism, marker, sections with time links, text-only, missing-frames note, 2000-char
    split, captions;
  - one page with captioned frames; a permit per request with the token-hash key; job parent beats
    default;
  - missing destination → FAILED before the token is read or any request is made;
  - token absent from errors/`repr`;
  - redelivery writes nothing; 503 on an append → resume without duplicates;
  - committed-503 create → reconciled, not duplicated; another job's same-titled page is never reused;
  - batching; 429 Retry-After; transport errors (GET vs POST uncertainty); upload 400 → warning +
    note; parent ID validation.
- `pytest tests/test_publish_stage.py tests/test_unit_publish.py tests/test_worker.py
  tests/test_summarize_stage.py tests/test_transcribe_stage.py tests/test_frames_stage.py`
  (22:22:37–22:25:07) → **61 passed** in 147.49 s (first run). The real-worker cases:
  - full pipeline → COMPLETED, one page, 2 captioned images, marker, the mailbox token (by hash),
    URL in `/results`, token not in logs;
  - text-only → COMPLETED_WITH_WARNINGS, 0 images; partial → 1 of 2;
  - job-specific parent;
  - 429 → outbox retry → one page;
  - committed 503 create → one page, `resumed`;
  - 401 → FAILED `NOTION_UNAUTHORIZED`;
  - SIGKILL mid-append → resume, marker once, 2 images;
  - SIGKILL after all writes before the commit → redelivery makes no write, same URL.
- **Full suite:** §7.

## 6. Real API verification
**Not performed, and not possible yet.** Publishing needs a user OAuth access token for the "Video
Notes Agent" integration. The OAuth flow (gateway code exchange + extension) is not implemented, and
`NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` / `NOTION_REDIRECT_URI` / `NOTION_DEFAULT_PARENT_ID` are
empty in `.env`. All Notion behaviour is verified against the fake API, which follows the documented
endpoints and limits; real-API compatibility (e.g. exact response fields under `Notion-Version:
2026-03-11`) is unverified.

## 7. Full suite
`MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -p no:warnings -q -rfE --tb=short`
(22:25:32–22:37:32) → **825 passed** in 717.59 s, 0 failed, 0 errors. Previous: 797; +28 new
(19 unit + 9 integration; the 61 above = 9 + 19 + 33 existing). RabbitMQ/PostgreSQL/Redis healthy with
0 restarts; no new diagnostics files.

## 8. Remaining limitations and unresolved issues
1. **No real Notion verification** (§6). This is the most important open point; the first real
   publish must be watched closely.
2. **Gateway OAuth + extension are not implemented** (Phase C): a real job cannot obtain a Notion
   token yet.
3. **Not deployed:** the dev stack still runs the Phase B Part 1 image. The dev image would need a
   rebuild (it was last rebuilt in Part 3, without PUBLISHING).
4. **Reconciliation** lists the parent's children once per attempt (paginated). A parent with very many
   pages costs more requests (each one rate-limited).
5. **Manual edits** to the job's page between a crash and the retry can break the prefix assumption
   (extra blocks → FAILED `NOTION_PAGE_CONFLICT`; fewer/changed blocks → the plan suffix is appended
   from the counted position).
6. **A database ID** as parent is not supported (pages only); the gateway accepts any UUID.
7. **Orphaned file uploads** from crashed attempts stay in Notion (they cannot be deleted; unattached
   ones expire).

## 9. Git
No Git operations were performed: no staging, commit, push or identity change. HEAD `1aa6863`;
0 staged; 41 status entries.
