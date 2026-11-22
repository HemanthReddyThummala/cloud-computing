# `gemini_429_once` live demonstration (runbook 5-L, ADR-22)

Date: 2026-11-22 IST (09:09 UTC onwards). The user approved this final demo, for video
`fyTxwIa-1U0` only.

Constraints:
- Claude never submits the job; the user clicks once;
- `MAX_ATTEMPTS=5`, empty namespace and the limits unchanged;
- no other fault;
- no Redis counter changes, no `.env` access, no Git;
- root `handover.md` untouched.

**What is demonstrated:**
1. The first SUMMARIZING request receives a simulated Gemini 429 (`RESOURCE_EXHAUSTED`, Retry-After
   20 s) **instead of** being sent to Google.
2. The production path classifies it as `GEMINI_RATE_LIMITED`, sets the shared cooldown and schedules
   a retry of ≥ 20 s.
3. The retry passes the normal Redis limiter and makes exactly one real Gemini request; the job
   completes with one Notion page.

## 1. Preflight (09:09:52 UTC): passed

| Check | Result |
|---|---|
| Workers | `2416170d2628` / `fcc150835a0d` on `36c573002412`, healthy, 0 restarts; namespace `''`, `DEMO_FAULTS_ENABLED=False`, `MAX_ATTEMPTS=5`, limits 4/200000/16, backoff 10,30,90, `RATE_LIMIT_MAX_WAIT_SEC` 60, `gemini-3.5-flash-lite` |
| Other services (baseline) | gateway `2252e6e419d4`, publisher `d75e4adfc751` (`2a01b00b9603`), postgres `84989aa9e22c`, rabbitmq `2664d216b80a`, redis `f88eabc19088`; healthy, 0 restarts |
| `/healthz` | ok; `live_workers` 2; consumers 2; `outbox_pending` 0 |
| Queues and jobs | work and retry queues 0; `jobs.dlq` 5; 19 jobs, 0 unfinished |
| Gemini (the deployed limiter's keys) | `ratelimit:gemini:rpd:2026-11-22` = **2/16** (14 free; this demo needs 2); RPM 0; **no Gemini cooldown**; no other cooldowns |
| Demo keys | only the inert job keys of the two finished frame demos |

## 2. Video suitability (from our own database)

"Session Vs JWT: The Differences You May Not Know!", `duration_sec` 419 (6:59). Earlier jobs:
- `cb5eb8e1` (2026-11-21): `COMPLETED`, 4/4 frames;
- `6141336d` (the crash demo): `COMPLETED`, 5/5 frames.

Both had 0 warnings.

## 3. Enable and arm

| UTC | Command | Result |
|---|---|---|
| — | `DEMO_FAULTS_ENABLED=true docker compose --dry-run up -d --no-deps --no-build --scale worker=2 worker` | only worker-1/worker-2; exit 0 |
| 09:10:15 | `DEMO_FAULTS_ENABLED=true docker compose up -d --no-deps --no-build --scale worker=2 worker` | exit 0; worker-1 `64c3e82ee6c3`, worker-2 `089ab83b6c01`, `36c573002412`, both healthy, 0 restarts |
| — | Verification | env and settings on both: `demo=True`, `max_attempts=5`, namespace `''`, limits 4/200000/16, backoff 10,30,90; logs 2 × "demo faults ENABLED", 2 × `(default)`, 0 errors; `/healthz` ok, 2 live workers |
| 09:10:35 | `docker compose exec worker python -m notesagent.ops.demo_faults arm gemini_429_once --video fyTxwIa-1U0` | `ARMED`, exit 0 |
| 09:10:35 | `… demo_faults status` | `demo:fault:video:fyTxwIa-1U0 = gemini_429_once (ttl 899s)`, so it expires at about 09:25 UTC |

A read-only watcher waits for the job, checks the binding, and captures the shared cooldown and
`GET /jobs/{id}` while the job waits.

## 4. The user's single click, the binding and the waiting state

| UTC | Event |
|---|---|
| 09:11:16.673 | The user clicked Take Notes once → job **`6649512b-c73b-4f9e-8f3b-f48f2d381689`**, the only job in the window |
| 09:11:17 | The watcher saw `demo:fault:job:6649512b… = gemini_429_once`; the video key was consumed (0); 1 "bound" log line |
| 09:11:20.926 | **Captured while waiting:** `ratelimit:cooldown:gemini` PTTL **19,125 ms** (the shared cooldown); the once-marker `…:gemini_429_done` set; limiter `rpd` 3; job `SUMMARIZING`, `attempts` 1; `GET /jobs/{id}` → `retry {"reason": "GEMINI_RATE_LIMITED", "attempt": 1, "max_attempts": 5, "next_attempt_at": "09:11:40.285"}` |
| — | The user confirmed the click ("i clicked"). Final verification began after that |

## 5. Result: the 429 behaviour was demonstrated; the job completed after an additional real Gemini 503

### Timeline (`job_events` + worker HTTP log lines)

| UTC | Worker | Event | External request |
|---|---|---|---|
| 09:11:16.696 | worker-2 | claimed → TRANSCRIBING | YouTube (metadata and transcript) |
| 09:11:20.014 | worker-2 | → SUMMARIZING | — |
| 09:11:20.285 | worker-2 | **`retry_scheduled` attempt 1: `GEMINI_RATE_LIMITED`, `http_status` 429, `provider_code` `RESOURCE_EXHAUSTED`, `delay_sec` 20.0** (1 × "demo fault injected", `retry_after` "20s"; 1 × "provider cooldown shared", `cooldown_sec` 20.0) | **none**: no Gemini HTTP line at that time |
| 09:11:40.332 | worker-1 | `retry_started` attempt 1 (after exactly the 20 s delay; the cooldown had expired; passed the limiter) | — |
| 09:11:42 | worker-1 | — | **`POST …gemini-3.5-flash-lite:generateContent` → real `503 Service Unavailable`** |
| 09:11:42.211 | worker-1 | `retry_scheduled` attempt 2: `GEMINI_HTTP_503`, `provider_code` `UNAVAILABLE`, backoff `gemini_unavailable`, `delay_sec` **180** (ADR-21) | — |
| 09:14:42.390 | worker-2 | `retry_started` attempt 2 (passed the limiter) | — |
| 09:14:48 | worker-2 | → EXTRACTING_FRAMES | **`POST …generateContent` → `200 OK`** |
| 09:14:52–09:14:59 | worker-2 | → PUBLISHING → **`COMPLETED`** (09:14:59.710) | Notion: 4 × `file_uploads` + 4 × `send`, **1 × `POST /v1/pages`**, 4 × `GET blocks/<id>/children`, all 200 |

### Verification against the user's checklist

| Requirement | Observed | Verdict |
|---|---|---|
| Exactly one simulated 429 | 1 × "demo fault injected" (`SUMMARIZING`, attempt 0); 1 retry event with 429/`RESOURCE_EXHAUSTED`; the once-marker blocked any second injection | ✅ |
| The simulated 429 did not call Google | no Gemini HTTP request between 09:11:20.014 and 09:11:40; the first Gemini HTTP line is at 09:11:42 | ✅ |
| The retry passed the normal Redis limiter | both later attempts took normal permits; the 20 s cooldown (`PTTL` 19,125 ms while waiting) had expired when attempt 1 ran; limiter `rpd` 2 → 3 → 4 → **5** (one permit per attempt, including the injected one as designed) | ✅ |
| Exactly one real Gemini request, successful | **2 real Gemini requests**: the first got a **real HTTP 503 from Google** (not injected) and the second succeeded (200). Exactly **one successful** request | ⚠ **deviation**: an external 503 forced one extra real request; it was handled by the production ADR-21 path (backoff 180 s) |
| Job completed | **`COMPLETED`**, `attempts` 2, 0 warnings, `frames` 4/4, `notion_page_url` present; 09:11:16 → 09:14:59 (3 min 43 s, mostly the 503 backoff) | ✅ |
| Exactly one Notion page, no duplicate | exactly **1** `POST /v1/pages` in the window; 1 job; 1 `PUBLISHING` checkpoint | ✅ |
| Warnings | none (`warnings` `[]`); 0 ERROR/CRITICAL/Traceback in worker logs | ✅ |
| Other jobs | none in the window (19 → 20 jobs); queues empty; `jobs.dlq` 5 | ✅ |

**Actual limiter usage (`ratelimit:gemini:rpd:2026-11-22`):** 2 before → **5** after (+3):
1. the injected 429 attempt (a permit is taken before the hook);
2. the real 503 request;
3. the real successful request.

The planned usage was +2; the extra permit is the real 503. 11/16 remain today. No Gemini cooldown
remains: the real 503 is not a 429 and sets none, as designed.

The Notion page was not opened by Claude.

## 6. Disabling and final verification (09:15 UTC)

| Step | Result |
|---|---|
| `disarm --video fyTxwIa-1U0` | "Nothing armed" (claimed). Remaining keys: `demo:fault:job:6649512b…` (+ `:gemini_429_done`) and the two earlier job keys; ≈ 2 h TTL, left to expire, inert |
| Dry run (variables unset) | only worker-1/worker-2 Recreate; exit 0 |
| Disable `docker compose up -d --no-deps --no-build --scale worker=2 worker` | exit 0 at 09:15:37 |
| Workers | worker-1 `c689d72e8a0a`, worker-2 `0929e6ddf9ee`; `36c573002412`; both **healthy**; 0 restarts; 2 × started, 0 "ENABLED", 0 errors |
| Effective config | **`DEMO_FAULTS_ENABLED=false`, `MAX_ATTEMPTS=5`**, namespace `''`, limits 4/200000/16 (env and settings) |
| Switch check | `arm` → `REFUSED`, exit 2 |
| `/healthz` | ok; `live_workers` 2; consumers 2; `outbox_pending` 0 |
| Other services | **identical to the preflight baseline** |
| Jobs | 20, 0 unfinished |

## Unresolved

- **Real Gemini 503s still occur intermittently** with `gemini-3.5-flash-lite` (also seen in
  `44df00c0`). They are handled correctly (ADR-21) but cost an extra permit and delay. They are outside
  our control.
- The pre-existing `settle()` TTL quirk on `ratelimit:gemini:tpm` (see `frames_all_demo.md`):
  harmless; a fix needs approval.
- A final verification command had a SQL quoting error; it was re-run correctly (20 jobs, 0
  unfinished).
