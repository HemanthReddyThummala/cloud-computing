# R9 burst-submission evaluation (3 clicks): result

Date: 2026-11-22 IST (10:00 UTC). This follows `burst_evaluation_preflight.md`; the user approved
("approved") the recommended **3-click** burst.

Constraints:
- Claude never submitted anything; the user clicked Take Notes once in each of three tabs;
- no configuration, Redis, container or limiter change;
- no Git;
- root `handover.md` untouched.

## 1. Pre-click re-check (09:59:49 UTC)

- `rpd:2026-11-22` 8/16; no cooldown; RPM window 0.
- Both workers healthy, 0 restarts.
- `/healthz` ok, 2 live workers.
- 21 jobs, 0 unfinished.

The state was unchanged since the preflight.

## 2. Method

A read-only monitor started at 10:00:04 UTC and sampled every ~2–3 s:
- jobs created since the window start and their states;
- `/healthz` `work_queue_messages`;
- the number of Gemini permits in the limiter's last 60 s (`ZCOUNT ratelimit:gemini:rpm`);
- whether the Gemini cooldown key exists.

It stopped when all 3 jobs were finished. Afterwards: the `jobs` and `job_events` rows, the worker HTTP
log lines and the limiter counter.

## 3. Results

| Job | Video (duration) | Submitted (UTC) | Finished (UTC) | Latency | Status | Attempts | Frames | Notion page |
|---|---|---|---|---|---|---|---|---|
| `98d54889` | `fyTxwIa-1U0` (419 s) | 10:01:52.813 | 10:02:12.369 | **19.6 s** | `COMPLETED` | 0 | 4/4 | ✔ |
| `37cb68ab` | `TlHvYWVUZyc` (388 s) | 10:01:55.231 | 10:02:17.972 | **22.7 s** | `COMPLETED` | 0 | 6/6 | ✔ |
| `c35f1c13` | `M988_fsOSWo` (383 s) | 10:01:57.406 | 10:03:29.844 | **92.4 s** | `COMPLETED` | 1 | 3/3 | ✔ |

- **Burst:** 3 submissions within **4.6 s** (10:01:52.813 → 10:01:57.406). Exactly 3 jobs were created
  (21 → 24).
- **Queueing:** jobs 1 and 2 were claimed immediately by worker-2 and worker-1. **Job 3 waited in
  `jobs.work.v2` for 15.0 s** (queue depth 1 sampled) until worker-2 finished job 1 (claimed at
  10:02:12.404).
- **Drained:** first submission → last completion = **97.0 s**. Queue and retry queues back to 0;
  0 unfinished.
- **No LLM 429:**
  - 0 Gemini 429 responses in the worker HTTP logs;
  - 0 `job_events` with `http_status` 429 or any `*RATE_LIMIT*` / `*COOLDOWN*` / `*DAILY_LIMIT*`
    reason;
  - 0 local limiter waits;
  - no Gemini cooldown key at any sample;
  - also 0 Notion 429s.
- **Gemini requests: 4**, all real:
  - 10:01:59 **200** (job 1, worker-2);
  - 10:02:04 **200** (job 2, worker-1);
  - 10:02:17 **503** (job 3, worker-2) → `retry_scheduled` `GEMINI_HTTP_503`, ADR-21 backoff **60 s**;
  - 10:03:20 **200** (job 3).
- At most **3** Gemini requests fell in any 60 s window (limit 4), so RPM pacing was **not** triggered,
  as expected for 3 clicks.
- **Limiter:** `ratelimit:gemini:rpd:2026-11-22` **8 → 12** (+4, one permit per request); **4/16
  remain**; no cooldown afterwards.
- **Notion:** exactly **3** `POST /v1/pages` (200), one per job; no duplicates.
- 0 warnings, 0 ERROR/CRITICAL/Traceback lines, 0 "demo fault" lines.

## 4. Assessment

**R9 burst (scope: 3 clicks): passed.**
- A burst of three Take Notes submissions within 4.6 s was queued across 2 workers and fully drained
  in 97 s with **no LLM 429**. Every job completed with one Notion page.
- The only delay was one real Gemini 503 (60 s backoff, ADR-21). Without it, the slowest job would have
  completed shortly after the queue wait.

**Not demonstrated by this run:**
- **Limiter pacing above RPM 4.** It needs a burst of ≥ 5 Gemini requests within 60 s and more daily
  budget (4/16 remain today). It is covered by the automated multi-process test
  `test_gemini_rpm_is_shared_by_independent_processes`.

## 5. Post-run state

- Workers `c689d72e8a0a` / `0929e6ddf9ee` healthy, 0 restarts.
- The other 5 services are identical to the baseline.
- `/healthz` ok, 2 live workers.
- Queues 0 (`jobs.dlq` 5); 24 jobs, 0 unfinished.
- No configuration, container or Redis change (the limiter counter changed only through the jobs'
  own permits).

## Unresolved

- A live RPM-pacing burst (≥ 5 clicks) has not been done; it would need a day with more free budget.
- Real Gemini 503s continue (today: 4 of 11 real requests).
