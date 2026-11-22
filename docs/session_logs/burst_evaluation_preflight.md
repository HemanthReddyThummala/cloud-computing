# R9 burst-submission evaluation: preflight only

Date: 2026-11-22 IST (09:56:56 UTC). **Preflight only, by instruction:**
- no job submitted, no click;
- no external YouTube, Gemini or Notion call;
- no change to configuration, environment, Redis, containers, database records or the limiter;
- no Git;
- root `handover.md` untouched.

The authoritative status document is intentionally **not** updated: the burst has not happened.

## 1. Commands (all read-only)

- `docker inspect` of the 5 services and 2 workers (ID, image, start time, restarts, health). Compared
  with the latency-evaluation preflight baseline using `diff`.
- In each worker: the env (`DEMO_FAULTS_ENABLED`, `MAX_ATTEMPTS`, `GEMINI_LIMITER_NAMESPACE`,
  `TRANSCRIPT_CHUNK_TOKENS`) and the loaded `get_settings()` values.
- Key names computed by the deployed limiter
  (`RateLimiter(...)._gemini_keys(now)`), then Redis `GET` / `ZCARD` / `ZCOUNT` / `ZRANGEBYSCORE` /
  `PTTL` / `EXISTS` / `SCAN` and `TIME`.
- `curl /healthz`; `rabbitmqctl list_queues`; `SELECT` counts on `jobs` and `outbox`.

## 2. Results

| Check | Result |
|---|---|
| Gateway / publisher / postgres / rabbitmq / redis | all **healthy**, 0 restarts; IDs `2252e6e419d4` / `d75e4adfc751` / `84989aa9e22c` / `2664d216b80a` / `f88eabc19088`; **identical** (ID, image, start time) to the baseline taken before the latency evaluation; `migrate` exited (0) |
| Workers | worker-1 `c689d72e8a0a`, worker-2 `0929e6ddf9ee`; image `36c573002412`; **healthy**; **0 restarts**; both started 09:15:37 UTC (the last demo-switch disable). No change since |
| `DEMO_FAULTS_ENABLED` | **false** (env) / False (settings) on both |
| `MAX_ATTEMPTS` | **5** on both |
| `GEMINI_LIMITER_NAMESPACE` | **empty** on both |
| `TRANSCRIPT_CHUNK_TOKENS` | **12000** on both |
| Other limiter settings | RPM 4, TPM 200,000, RPD 16; `RATE_LIMIT_MAX_WAIT_SEC` 60; backoff 10,30,90; Gemini 503 backoff 60,180,600; model `gemini-3.5-flash-lite` |
| **RPD** `ratelimit:gemini:rpd:2026-11-22` | **8 used / 16 → 8 remaining** |
| **RPM** `ratelimit:gemini:rpm` | 0 entries (0 in the last 60 s) |
| **TPM** `ratelimit:gemini:tpm` | 1 stale entry, **0 in the current 60 s window** (0 tokens). `PTTL -1`: the known, harmless `settle()` TTL quirk (`frames_all_demo.md`) |
| Gemini cooldown | **none** (`ratelimit:cooldown:gemini` absent; 0 cooldown keys) |
| Other keys | `ratelimit:gemini:rpd:2026-11-21` (previous day, not used); 4 inert job-bound demo keys (switch off) |
| `/healthz` | ok; `live_workers` 2; `work_queue_consumers` 2; `outbox_pending` 0 |
| Queues | `jobs.work.v2` 0 (2 consumers); `jobs.retry.{5,10,30,90}s` 0; `jobs.dlq` 5; `jobs.dlq.audit` 0 (2 consumers); parked 0 |
| Jobs | **21 total, 0 unfinished**; latest `b3a24d11` (09:47:03); outbox unpublished 0 |

**Today's Gemini usage (the 8 used permits):**

| Job | Permits |
|---|---|
| `82c49d33` | 1 |
| `74779edd` | 1 |
| `6649512b` | 3 (simulated 429 + real 503 + success) |
| `b3a24d11` | 3 (503, 503, success) |

That is **7 real requests today, 3 of them HTTP 503** (≈ 43 %).

## 3. Burst-size analysis

**What the burst can show:**
- **2 workers, prefetch 1:** 2 jobs are processed at a time and the rest wait in `jobs.work.v2`.
  Draining the queue without an LLM 429 is the R9 requirement.
- **Exercising the limiter's RPM pacing needs ≥ 5 Gemini requests in 60 s** (RPM 4). Exactly 4 reach
  the limit without exceeding it; 3 stay below it.

**Cost per job (short, single-chunk video):**
- normally **1** permit;
- each real 503 costs **+1** permit (ADR-21: retries after 60 s, then 180 s, then 600 s);
- up to `MAX_ATTEMPTS` 5 permits for a job that keeps getting 503s.

**What happens if the budget runs out:**
- The limiter refuses locally (`GEMINI_DAILY_LIMIT_REACHED`), so Google is **not** called and there is
  no 429.
- The affected job then retries and dead-letters, as `44df00c0` did on 2026-11-21.
- This protects the quota, but a dead-lettered job would spoil a "burst drains" result.

| Clicks | Minimum permits | Spare for 503 retries (of 8) | Expected permits at today's ≈ 43 % 503 rate* | Shows RPM pacing? |
|---|---|---|---|---|
| 5 | 5 | 3 | ≈ 8.8 (exceeds 8) | yes |
| 4 | 4 | 4 | ≈ 7.0 | reaches RPM 4, doesn't exceed it |
| **3** | **3** | **5** | **≈ 5.3** | no (queueing and draining only) |

\* Expected attempts per job = 1 / (1 − 0.43) ≈ 1.75. This is a rough estimate from a small sample.

**Recommendation: 3 clicks.**
- 3 short videos that have completed before with captions, e.g. `M988_fsOSWo` (6:23), `TlHvYWVUZyc`
  (6:28) and `fyTxwIa-1U0` (6:59). Each is 1 chunk and 1 normal request.
- Clicked within a few seconds (one per tab).
- **Why 3:** even 5 consecutive 503s across the burst still fit (3 + 5 = 8), so no job should hit the
  local daily limit.
- **Limitation:** it shows queueing across 2 workers and draining without an LLM 429. It does **not**
  push the limiter past RPM 4. RPM pacing across independent processes stays covered by the automated
  test `test_gemini_rpm_is_shared_by_independent_processes`.
- **Alternative:** a 5–6 click burst that also shows live RPM pacing should wait for a day on which the
  limiter shows more free budget. It must not be run today; it would need the limits loosened or
  another namespace, which is not permitted.

## 4. Risks and concerns

- **Real Gemini 503s are frequent today** (3 of 7 requests). Each one adds a 60–180 s wait, so
  draining could take minutes rather than seconds. A job hit by repeated 503s could take up to about
  24 min to dead-letter.
- **Burst capacity is small** (8 permits), so the test can't demonstrate RPM pacing today.
- **The extension creates one job per click** (a fresh idempotency key each time), so accidental
  double-clicks add jobs and consume permits.
- **No submissions from other browsers or people** should happen during the window: the unplanned
  4-job burst on 2026-11-21 shows the risk.
- The harmless TPM `settle()` TTL quirk is still present (not changed).

## 5. Confirmation

No job was submitted, no click or external call was made, and no configuration, environment, Redis,
container, database or limiter state was changed. The status document was not updated.
