# Evaluation: end-to-end latency near the 20-minute cap (Option A, no configuration change)

Date: 2026-11-22 IST (09:45 UTC onwards). The user approved **one** real job, a latency measurement
only, with no configuration change (`TRANSCRIPT_CHUNK_TOKENS` stays 12,000).

Constraints:
- Claude never submits; the user clicks once;
- no alternate key or namespace;
- no Redis changes, no `.env` access, no Git;
- root `handover.md` untouched.

## 0. Why a different video, and why chunk-and-merge is not expected

- **First proposed video:** `favi7avxIag` was rejected because it is 932 s (15:32), outside 18–20
  minutes.
- **Offline calibration** (production `chunk_segments` / `estimate_tokens` on its stored TRANSCRIBING
  checkpoint; no external call): 343 segments, 16,705 characters, **6,029 estimated tokens**, budget
  12,000, so **1 chunk**. That is about 388 tokens per minute, about 7,800 at 20:00.
- **Consequence:** an ordinary video under 20 minutes is not expected to split. The user chose
  Option A: measure latency and record the chunk count honestly.

## 1. Preflight (09:45:47 UTC): passed

| Check | Result |
|---|---|
| Video `rdb2ki4iGuo` (yt-dlp metadata via the project's `YouTubeClient.video_info`, inside a worker; **1 read-only YouTube metadata request**, no download) | "Lec-49: Subnetting in Classful Addressing with Examples in Hindi \| Computer Networks"; **`duration` 1156 s = 19:16**; ≤ `MAX_VIDEO_DURATION_SEC` 1200 ✔; within 18–20 min ✔; `not_live`, `public`, age_limit 0 |
| Existing records | 0 jobs and 0 idempotency keys for `rdb2ki4iGuo` |
| Idempotency | The gateway deduplicates by `Idempotency-Key` only (`submission.py`), and every Take Notes click sends a fresh `vna:<videoId>:<uuid>`, so one click → one new job. There is no per-video dedup and no conflict |
| Workers | `c689d72e8a0a` / `0929e6ddf9ee` on `36c573002412`, healthy, 0 restarts; `DEMO_FAULTS_ENABLED=False`, `MAX_ATTEMPTS=5`, namespace `''`, **`transcript_chunk_tokens` 12000**, limits 4/200000/16, `gemini-3.5-flash-lite`, `TRANSCRIPT_LANGUAGES=en`, `MAX_FRAMES=8` |
| Other services (baseline) | gateway `2252e6e419d4`, publisher `d75e4adfc751`, postgres `84989aa9e22c`, rabbitmq `2664d216b80a`, redis `f88eabc19088`; healthy, 0 restarts |
| `/healthz` | ok; `live_workers` 2; consumers 2; `outbox_pending` 0 |
| Queues and jobs | work and retry queues 0; `jobs.dlq` 5; 20 jobs, 0 unfinished |
| Gemini | `ratelimit:gemini:rpd:2026-11-22` = **5/16** (11 free); RPM 0; **no cooldown**; no other cooldowns |
| Demo keys | 4 inert job-bound keys from the finished demos (the switch is off; never read) |

**Risk noted, not checked:** the lecture is in Hindi. The workers prefer `en` transcripts but use any
available transcript otherwise. If the video had no captions at all, the job would end `FAILED
YOUTUBE_TRANSCRIPT_UNAVAILABLE` before Gemini. Checking this would need another YouTube request, which
was not part of the approval.

## 2. Measurement method

- **Submission time** = `jobs.created_at`, the gateway's accept time, milliseconds after the click.
  **Completion time** = `jobs.finished_at`. **Latency** = the difference, computed in PostgreSQL.
- **Stage timings** come from `job_events`.
- **Chunks:**
  - re-run the production chunker offline on the job's TRANSCRIBING checkpoint (read-only);
  - count the worker log lines `HTTP Request: POST …generateContent` (1 = single chunk; *n* + 1 =
    *n* chunks + merge).
  - The "Transcript part k of n" prompt header is not logged.
- **Gemini usage:** the limiter `rpd` counter before and after.
- **Notion:** `POST /v1/pages` log lines and `notion_page_url` presence.

## 3. The user's single click

- The user clicked Take Notes once and confirmed ("i clicked").
- Job **`b3a24d11-d77e-4686-aabb-a99d22126175`** was the only job created in the window (jobs 20 → 21).
- 0 "demo fault" log lines (switch off).

## 4. Measurements (verified)

| Measure | Value | Source |
|---|---|---|
| Submission (`created_at`) | **2026-11-22 09:47:03.312 UTC** | `jobs` |
| Completion (`finished_at`) | **09:51:30.953 UTC** | `jobs` |
| **End-to-end latency** | **267.641 s (4 min 27.6 s)** | `finished_at − created_at` (PostgreSQL) |
| Of which Gemini 503 backoff waits | **240.346 s** (60.176 + 180.170) | `job_events` |
| Processing time excluding those waits (derived) | **27.295 s** | 267.641 − 240.346 |
| Video duration | **1156 s (19:16)** (`duration_exact` 1156.0); the last transcript segment starts at 1154.7 s | TRANSCRIBING checkpoint |
| Transcript | **English, manually created** (`language_code en`, `is_generated` false; despite "Hindi" in the title); **612 segments**, 23,953 characters | checkpoint |
| Estimated tokens | **8,801** (production `estimate_tokens`; about 457 tokens/min) | offline re-run of the production chunker on the checkpoint (read-only) |
| **Chunks** | **1** (budget 12,000) | `chunk_segments(…, chunk_budget(settings))` |
| **Merge request** | **Did not run.** There was only one successful `generateContent`; a merge needs ≥ 2 chunks | log lines + chunker |
| Gemini HTTP requests | **3**: 09:47:10 **503**, 09:48:13 **503**, 09:51:17 **200 OK**; all real responses from Google | worker HTTP log lines |
| Limiter `ratelimit:gemini:rpd:2026-11-22` | **5 → 8 (+3)**, one permit per request; no cooldown afterwards (503 sets none) | Redis `GET` before and after |
| Final state | **`COMPLETED`**, `attempts` 2, `infra_attempts` 0, **0 warnings**, no error | `jobs` |
| Frames | **4 requested / 4 published** | `result` |
| Notion | **1 × `POST /v1/pages`** (200), 4 × `file_uploads` + 4 × `send` (200), 2 × `GET blocks/<id>/children`; `notion_page_url` present | log lines, `result` |

### Stage timeline (`job_events`, seconds after submission)

| t (s) | Event |
|---|---|
| 0.000 | submitted (QUEUED) |
| 0.049 | claimed by worker-1 → TRANSCRIBING |
| 2.964 | → SUMMARIZING (transcribing ≈ 2.9 s) |
| 7.353 | `retry_scheduled` attempt 1: **`GEMINI_HTTP_503` / `UNAVAILABLE`**, backoff `gemini_unavailable` **60 s** |
| 67.529 | `retry_started` (worker-2) |
| 70.115 | `retry_scheduled` attempt 2: **`GEMINI_HTTP_503`**, backoff **180 s** |
| 250.285 | `retry_started` (worker-1) |
| 254.354 | → EXTRACTING_FRAMES (the successful Gemini request ≈ 4.1 s) |
| 262.246 | → PUBLISHING (frames ≈ 7.9 s) |
| 267.641 | → **COMPLETED** (publishing ≈ 5.4 s) |

## 5. Conclusions

- **Latency near the cap (R9):** a 19:16 video completed in **267.6 s**. **240.3 s** of that was ADR-21
  backoff after two real Gemini 503s, which are outside our control. The pipeline's own processing
  (transcript, one Gemini summary, 4 frames, Notion with 4 images) took about **27.3 s** in total.
  That figure is derived by subtracting the recorded backoff delays, not measured as a separate run.
- **Chunk-and-merge NOT exercised:** at 8,801 estimated tokens the transcript fits the 12,000-token
  budget in one chunk, so no merge request ran. **The chunk-and-merge path remains covered by automated
  tests only.**
- **Gemini 503s recur** with `gemini-3.5-flash-lite`. This is the third observed job with real 503s
  (also `44df00c0` and `6649512b`). They were handled correctly, but they dominate latency when they
  occur.

## 6. Post-job state

- Both workers are healthy and unchanged (`c689d72e8a0a` / `0929e6ddf9ee`, 0 restarts).
- The other five services are identical to the preflight baseline.
- `/healthz` ok, with 2 live workers; work and retry queues 0; `jobs.dlq` 5.
- 21 jobs, 0 unfinished.
- No configuration, container or Redis change was made (the limiter counter changed only through the
  job's own permits).

## Unresolved

- Chunk-and-merge with a real transcript: not exercised (tests only). Option B (a temporary lower
  `TRANSCRIPT_CHUNK_TOKENS`) would need separate approval.
- A latency measurement free of 503s is not available. The derived 27.3 s is an estimate of the
  no-503 path.
- One of my log-formatting commands failed (`sed` syntax); it was re-run correctly. No effect on the
  data.
