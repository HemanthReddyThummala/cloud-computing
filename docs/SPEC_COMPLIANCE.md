# Specification compliance: Distributed Multimodal Video Note-Taking Agent

This document maps every requirement of `PROJECT_SPEC.pdf` (§1–§9) to its implementation and the
evidence for it. Detailed, dated evidence is in `docs/session_logs/`; design decisions and their
justification (spec §10) are in `docs/decisions.md` (ADR-01 … ADR-23). The live project status is in
`docs/PROJECT_STATUS_AND_HANDOVER.md`.

**Evidence levels**

| Mark | Meaning |
|---|---|
| **Live** | Shown on the running stack with real YouTube, Gemini and Notion |
| **Tests** | Automated tests on real PostgreSQL, RabbitMQ and Redis, with fake external providers |
| **Gap** | Not fully shown; the reason is given |

**Latest test runs (2026-11-22):**
- backend **951 passed, 0 failed** (830.7 s);
- extension **94 passed, 0 failed**.

## §1 Required components

| Requirement | Implementation | Evidence |
|---|---|---|
| Four independent processes/containers | Chrome extension (MV3); `gateway` (FastAPI); RabbitMQ; `worker` × N; plus `outbox-publisher`, PostgreSQL and Redis (`docker-compose.yml`) | Live |
| The gateway does no pipeline work | `POST /jobs` only validates, stores the job and an outbox event in one transaction, then returns 202 (ADR-02, ADR-04) | Tests, Live |
| Durable broker, at-least-once, redelivery on crash | RabbitMQ quorum queues (ADR-09); manual ack only after a stage's result is committed, idempotent stages (ADR-05, ADR-17) | Tests; Live (crash demo) |
| ≥ 2 worker processes | `worker` service, 2 replicas by default; scaled 2 → 3 → 2 live | Live (`scaling_rehearsal_success.md`) |

## §2 Browser extension

| Requirement | Implementation | Evidence |
|---|---|---|
| "Take Notes" button in the player toolbar on watch pages | `extension/content.js` | Live (user); Tests |
| Keeps working across video-to-video navigation (SPA) | listens to `yt-navigate-finish` + `popstate` and resyncs the single button | Tests (3 navigation tests in `content.test.js`) |
| One-time Notion OAuth; token stored; no re-prompt | `background.js` / `lib/auth.js`; the gateway does the code exchange; the sealed token is stored in `chrome.storage.local` (ADR-18, ADR-19) | Live (real sign-in; many jobs without a re-prompt) |
| Submits jobs, relays status to the popup | `lib/jobs.js` tracker: idempotency key, polling (3 s; 10 s while waiting for a retry) | Live; Tests |
| Popup: Queued → Transcribing → Summarizing → Extracting Frames → Publishing → Done; "View in Notion"; clear failures and warnings; never raw JSON | `lib/popup-view.js` (readable reasons, warnings, safe Notion link) | Live (user); Tests (94) |

## §3 Backend pipeline

| Requirement | Implementation | Evidence |
|---|---|---|
| Timestamped transcript + true duration; Constraint A enforced | `worker/transcribe.py`: yt-dlp metadata → `check_duration` **before** the transcript; youtube-transcript-api | Live (> 20 min rejected; 19:16 accepted) |
| LLM summary bullets + flagged timestamps + **one caption per timestamp** | `worker/summarize.py` (Gemini `gemini-3.5-flash-lite`, structured JSON, grounded timestamps) | Live (many jobs) |
| Chunk long transcripts, merge partial results in order | `chunk_segments` + merge request (`TRANSCRIPT_CHUNK_TOKENS`, default 12,000) | Tests. **Gap:** not exercised live; ordinary videos ≤ 20 min fit one chunk (19:16 → 8,801 estimated tokens) |
| Capture one real frame per timestamp from the video stream | `worker/frames.py`: FFmpeg `-ss <t> -frames:v 1` on the downloaded stream | Live (e.g. 4/4, 6/6, 7 requested) |
| Upload frames; page with headings, bullets, captioned images | `worker/publish.py` (Notion file uploads; image block caption = timestamp link + LLM caption) | Live |

## §4 Job lifecycle

All nine states (`common/states.py`) and the transitions in the spec table are enforced with
compare-and-set updates and recorded in `job_events`.

**Evidence:**
- **Tests;**
- **Live:** every state observed, including `COMPLETED_WITH_WARNINGS`, `FAILED` and `DEAD_LETTERED`.

## §5 REST API (`docs/api.md`)

| Requirement | Evidence |
|---|---|
| `POST /jobs` with `youtube_url`, `notion_token`, optional `notion_parent_id`; **202** + `Location: /jobs/{id}`; returns immediately | Tests, Live |
| Invalid input → 4xx `{"error": {"code", "message"}}` | Tests |
| Omitted `notion_parent_id` → default destination, stated in the README (`NOTION_DEFAULT_PARENT_ID`) | Live |
| `GET /jobs/{id}`: read-only, idempotent; unknown → **404** | Tests |
| `GET /jobs/{id}/results`: before terminal → **409**; on success an absolute Notion URL, frame counts and warnings | Tests, Live |

## §6 Required failure handling (live)

Live procedures are in `docs/FAILURE_DEMONSTRATION_RUNBOOK.md`.

| Failure mode | Evidence | Notes |
|---|---|---|
| **HTTP 429 → backoff, job shown waiting** | **Live (Gemini):** job `6649512b`, a 429 injected at the provider boundary, shared 20 s cooldown, popup "Waiting to retry … (attempt 1 of 5)", then `COMPLETED`. **Tests (all three providers):** YouTube, Gemini, Notion, frame download, cross-worker cooldown (runbook 5-A) | **Gap:** a real 429 cannot be produced on demand, so the live demo uses the ADR-22 demo switch, which raises the provider's own exception type; all handling after it is production code. YouTube and Notion 429 are shown with the automated tests, not live |
| **Worker crash mid-job → reassigned, not lost, no duplicate page** | **Live:** job `6141336d`, the worker SIGKILLed during EXTRACTING_FRAMES, resumed by the other worker → `COMPLETED`, exactly one Notion page | A crash *during* PUBLISHING (page reconciliation) is covered by tests only |
| **Graceful degradation** | **Live:** `82c49d33` 4/7 frames → `COMPLETED_WITH_WARNINGS`; `74779edd` 0/4 frames → **text-only page** | — |
| **DLQ after a bounded, configurable number of attempts** (ADR-11, ADR-12) | **Live:** `129af1de` → `DEAD_LETTERED` after 3 attempts, copy in `jobs.dlq`; real 503 / daily-limit jobs also dead-lettered (5 in `jobs.dlq`) | `MAX_ATTEMPTS` and `BACKOFF_SCHEDULE_SEC` are configurable. **Design choice (ADR-14 D-1):** private or unavailable videos end `FAILED` immediately (non-retryable), so the DLQ is demonstrated with a retryable fault |

## §7 Hard constraints

| Constraint | Implementation | Evidence |
|---|---|---|
| **A**: ≤ 20 min supported; longer rejected deterministically | `MAX_VIDEO_DURATION_SEC=1200` checked before any transcript work | Live: > 20 min → `FAILED VIDEO_TOO_LONG`; 19:16 → `COMPLETED` |
| **B**: only real frames, each with an LLM caption; a failed frame is omitted, never substituted | FFmpeg capture only (no image generation); every image block carries the LLM caption; failed frames → warning + omission | Live (frame demos: omitted, not substituted); Tests |

## §8 Deployment and deliverables

| Deliverable | Where |
|---|---|
| Unpacked extension, no build step | `extension/` (Chrome → `chrome://extensions` → Developer mode → Load unpacked) |
| Gateway, broker, ≥ 2 workers in one compose file; `--scale worker=N` | `docker-compose.yml`; README §7 |
| README: base URL, default Notion destination, every required environment variable | `README.md`: `http://localhost:8000`; `NOTION_DEFAULT_PARENT_ID`; `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_RPM/TPM/RPD_LIMIT`, `NOTION_RPS_LIMIT`, `NOTION_CLIENT_ID/SECRET/REDIRECT_URI`, `RABBITMQ_URL`, `MAX_ATTEMPTS`, `BACKOFF_SCHEDULE_SEC`, … and `.env.example` |
| Health check | `GET /healthz`: gateway, PostgreSQL (state store), RabbitMQ (broker), Redis (limiter), live workers |

**Gap:** the README has not been followed once from a clean checkout on another machine.

## §9 Evaluation criteria (our measurements)

| Criterion | Result |
|---|---|
| Client experience | Popup stage sequence, readable failures and warnings, "View in Notion" |
| Latency near the 20-min cap | 19:16 video (`b3a24d11`): **267.6 s** end to end, of which **240.3 s** was ADR-21 backoff after two real Gemini HTTP 503s. The remaining processing took ≈ **27.3 s** (derived) |
| Queue resilience (burst) | 3 Take Notes clicks on 3 videos within 4.6 s; queued across 2 workers; all `COMPLETED`; drained in 97 s; **0 LLM 429s** |
| Fault tolerance | Crash demo: the killed job was resumed by the other worker and completed with exactly one Notion page |
| Constraints A/B | As in §7 |

**How the global LLM rate limit is coordinated:** a Redis atomic Lua limiter shared by all workers (RPM,
TPM, RPD per Pacific day; shared 429 cooldown) — ADR-13, ADR-23.

**Gap:** live pacing above 4 requests per minute (≥ 5 Gemini requests within 60 s) has not been shown;
it is covered by a multi-process test.

## Known external factor

Google's Gemini API returned HTTP 503 `UNAVAILABLE` intermittently during the evaluation (several jobs).
The system handles it as retryable (ADR-21: 60 s / 180 s / 600 s backoff), but it can dominate
end-to-end latency when it occurs.
