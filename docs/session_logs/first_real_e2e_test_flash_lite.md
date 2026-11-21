# First real end-to-end test after the Flash-Lite switch

Date: 2026-11-21. One real video job was submitted through Chrome's Video Notes Agent extension.
No retry or second job was submitted.

## Objective and preflight

Test one short public YouTube video through the production Chrome extension → gateway → outbox/broker
→ workers pipeline after switching the configured model to `gemini-3.5-flash-lite`.

The selected video was *What is Microlearning? [2-Minute Explainer]*:

- YouTube video ID: `KlbsuPAibfY`
- URL: `https://www.youtube.com/watch?v=KlbsuPAibfY`
- Duration shown by YouTube: 2 minutes 32 seconds (the worker later recorded 153 seconds)
- Captions were visible during playback.

Before submission:

- Both workers were running and healthy, and each reported
  `gemini-3.5-flash-lite` from loaded application settings.
- `GET http://localhost:8000/healthz` returned HTTP 200:
  gateway/PostgreSQL/RabbitMQ/Redis checks were OK; two live workers and two work-queue consumers
  were reported.
- No production configuration, source code, environment file, rate limit, or running service was
  changed.

## Files and records inspected

- `docs/PROJECT_STATUS_AND_HANDOVER.md`
- `docs/session_logs/gemini_35_flash_lite_production_switch.md`
- `extension/content.js` (Take Notes submits the current YouTube URL through the extension
  background)
- `backend/notesagent/worker/stages.py`
- `backend/notesagent/worker/transcribe.py`
- `backend/notesagent/worker/summarize.py`
- `backend/notesagent/worker/frames.py`
- `backend/notesagent/worker/publish.py`
- `backend/notesagent/common/http_errors.py`
- `backend/migrations/001_init.sql` (job audit and stage-checkpoint tables)

## Commands and workflow

Worker settings and container health were checked without printing environment variables:

```powershell
docker inspect notes-agent-worker-1 notes-agent-worker-2
docker exec notes-agent-worker-1 python -c "from notesagent.common.config import get_settings; print(get_settings().gemini_model)"
docker exec notes-agent-worker-2 python -c "from notesagent.common.config import get_settings; print(get_settings().gemini_model)"
Invoke-WebRequest -Uri 'http://localhost:8000/healthz' -Method Get -TimeoutSec 10 -UseBasicParsing
```

The video was opened in Chrome, then the extension's in-player **Take Notes** action was used. The
job travelled through the normal gateway, outbox/broker and workers. No direct job API submission,
broker publish, rate-limit bypass, or Redis modification was used.

The UI action was interrupted once by concurrent user input. The UI was re-observed before continuing;
the database was checked read-only afterward. Exactly one job record matched this video ID. No manual
job retry or second submission was made.

Read-only job and checkpoint inspection used the running gateway's database connection, printing only
job status, selected structured-output counts/timestamps, and frame-integrity booleans. Transcript
text, summary text, image bytes, credentials, API keys, and `.env` values were not printed. The
checkpoint checks decoded the stored frame payloads in memory and verified JPEG signatures, byte
lengths, and SHA-256 values.

After the run, `GET http://localhost:8000/healthz` was repeated and returned HTTP 200 with two live
workers and two consumers. Both workers still reported `gemini-3.5-flash-lite`.

## Job and stage results

- **Job ID:** `574eb00f-f7df-40a7-9bb4-1b235458d330`
- **Matching jobs for this video:** 1
- **Start / finish:** 2026-11-21 17:37:55 IST / 17:38:06 IST (about 10.7 seconds)
- **Final status:** `FAILED`, progress 85%
- **Stage path:** `QUEUED` → `TRANSCRIBING` → `SUMMARIZING` → `EXTRACTING_FRAMES` →
  `PUBLISHING` → `FAILED`

| Stage | Result |
|---|---|
| TRANSCRIBING | **Passed.** YouTube metadata and transcript were retrieved; 39 timestamped segments; recorded duration 153 seconds. |
| SUMMARIZING | **Passed.** `gemini-3.5-flash-lite`; 1 chunk and 1 Gemini request; non-empty summary; 3 key points; 3 sections; 4 visual moments. Section starts were 0.089, 24.06, and 38.06 seconds; visual moments were at 2.21, 26.21, 40.21, and 110.81 seconds. All matched transcript segment starts. |
| EXTRACTING_FRAMES | **Passed.** 4 frames requested, 4 extracted, 0 failed. All four stored payloads were JPEGs with matching byte lengths and SHA-256 checks; all frame timestamps matched the visual moments, and each had non-empty caption/reason metadata. |
| PUBLISHING | **Failed.** Job error code `NOTION_OBJECT_NOT_FOUND`; sanitized message: “The Notion page was not found or not shared with the connection.” The job had no per-job Notion parent ID and no successful result/page URL. |

The failure audit did not retain the raw Notion HTTP status. The application classifier maps HTTP 404
to `NOTION_OBJECT_NOT_FOUND`, but this report treats the stored sanitized code/message as the observed
result. The evidence does not distinguish an absent destination page from one not shared with the
integration.

## Outcome and unresolved work

This was a **partial end-to-end success**, not a completed job: transcription, one structured
Flash-Lite request, and real frame extraction passed; Notion publishing failed. The checkpoint
contains four integrity-verified extracted JPEGs, but no successful Notion page result exists, so
Notion page contents, timestamps, frame embeds, and the no-duplicate-page behavior could not be
verified.

The configured Notion default parent should be checked for existence and sharing with the Notion
integration before another test. No retry or additional job was made; report this outcome and obtain
authorization before any further attempt.

No source/configuration change, production-setting change, limiter change, container restart, Git
staging, commit, or push was made. Root `handover.md` was untouched. The authoritative status update
is recorded in `docs/PROJECT_STATUS_AND_HANDOVER.md`.
