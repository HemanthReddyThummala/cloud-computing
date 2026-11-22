# Failure demonstration runbook (spec §6)

Spec §6: the system "must demonstrably handle, **live**", four failure modes a grader may trigger
deliberately:
1. HTTP 429 from any of the three APIs (wait and back off, not fail);
2. worker crash (no job lost, no duplicate Notion page);
3. graceful frame degradation (`COMPLETED_WITH_WARNINGS`, text-only if all frames fail);
4. a dead-letter queue after a bounded, configurable number of attempts.

Scaling is required by §8.

**Status of this document:**
- prepared 2026-11-22 from the code, Compose file, tests and ADRs;
- **nothing below has been rehearsed yet** (no demo was run while writing it);
- every "Live" step that submits a Take Notes job makes real YouTube / Gemini / Notion calls and
  **needs the user's approval at demo time**.

Each demo is marked:
- **A — Automated proof:** integration tests with real PostgreSQL, RabbitMQ and Redis and real worker
  processes (killed with real SIGKILLs); only YouTube, Gemini and Notion are fakes. Safe to run any
  time: isolated database `notes_test`, vhost `notes_test`, Redis db 15.
- **L — Live on the running stack:** real infrastructure and real external APIs.

## Readiness at a glance

| # | Demonstration | A (automated) | L (live) |
|---|---|---|---|
| 1 | Worker crash recovery, no duplicate page | ✅ ready | ✅ **rehearsed successfully 2026-11-22** (1 real job; `docs/session_logs/crash_recovery_rehearsal_success.md`) |
| 2 | Dead-letter queue | ✅ ready | ✅ **rehearsed successfully 2026-11-22** via the demo fault switch (`dlq`; job `129af1de`, 0 external calls; `docs/session_logs/dlq_demo_rehearsal_success.md`) |
| 3 | Graceful frame degradation | ✅ ready | ✅ **`frames_partial` rehearsed successfully 2026-11-22** (job `82c49d33`: 7 requested / 4 published, 3 warnings; `docs/session_logs/frames_partial_demo.md`); ✅ **`frames_all` rehearsed successfully 2026-11-22** (job `74779edd`: 4 requested / 0 published, text-only page; `docs/session_logs/frames_all_demo.md`) |
| 4 | Worker scaling | ✅ ready (concurrency test) | ✅ **rehearsed successfully 2026-11-22** (2 → 3 → 2, no job; see `docs/session_logs/scaling_rehearsal_success.md`) |
| 5 | HTTP 429 handling (YouTube, Gemini, Notion) | ✅ ready (each provider) | ✅ **Gemini 429 rehearsed successfully 2026-11-22** (`gemini_429_once`, job `6649512b`: 1 simulated 429, no Google call, 20 s shared cooldown, `COMPLETED` with 1 Notion page; a real Gemini 503 on the retry added one 180 s ADR-21 backoff; `docs/session_logs/gemini_429_once_demo.md`); YouTube/Notion 429 via 5-A |

---

## 0. Common preparation

```bash
cd "<project root>"
export MSYS_NO_PATHCONV=1                 # Git Bash on Windows only
docker compose ps -a                      # all healthy; migrate exited (0)
curl -s http://localhost:8000/healthz     # "status":"ok", "live_workers":2, "work_queue_consumers":2
docker compose --profile test build tests # once, for the automated (A) demos
```

To run an automated demo:

```bash
docker compose --profile test run --rm --no-deps tests pytest -v -p no:warnings <test ids>
```

Evidence queries for live demos (read-only; replace `<JOB>`):

```bash
q() { docker exec notes-agent-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"' _ "$1"; }
q "SELECT id, video_id, status, attempts, error->>'code' AS code, error->>'last_error_code' AS last, result->>'notion_page_url' AS page FROM jobs ORDER BY created_at DESC LIMIT 5"
q "SELECT created_at, event_type, from_status, to_status, worker_id, detail FROM job_events WHERE job_id='<JOB>' ORDER BY id"
docker exec notes-agent-rabbitmq-1 rabbitmqctl list_queues -p / name messages messages_unacknowledged consumers
```

(The helper was checked against the running database; credentials are read inside the container and are
never printed.)

**Quota note:** each Take Notes job normally uses one Gemini request (limit 16/day in our limiter). A job
redelivered after its SUMMARIZING checkpoint does not call Gemini again.

---

## 1. Worker crash recovery

**Mechanism** (ADR-05/11):
- manual ack only after a stage's result is committed;
- per-job advisory lock and compare-and-set (CAS) state changes;
- stage checkpoints, so a redelivered job resumes at the stage it was in;
- PUBLISHING reconciles before every write: it finds its own page by the job marker in the first
  block, so a page is never created twice.

### 1-A Automated (ready)

```bash
docker compose --profile test run --rm --no-deps tests pytest -v -p no:warnings \
  tests/test_worker.py::test_sigkill_mid_stage_job_is_recovered_by_another_worker \
  tests/test_frames_stage.py::test_crash_during_download_repeats_only_the_frames_and_cleans_up \
  tests/test_publish_stage.py::test_crash_mid_append_resumes_without_a_second_page \
  tests/test_publish_stage.py::test_crash_after_all_writes_before_commit_writes_nothing_more \
  tests/test_transcribe_stage.py::test_crash_during_transcribing_redelivers_and_stores_one_checkpoint
```

**Expected:** all pass. They SIGKILL a real worker process mid-stage. The other worker picks up the
redelivered message, the job completes, and the fake Notion holds exactly one page.

### 1-L Live (ready; 1 real job; needs approval)

1. Pick a ~10-minute public video with captions that has **not** been published before (or note the
   number of child pages under the "Video Notes" page first). Longer videos give a longer
   EXTRACTING_FRAMES window.
2. Terminal 1: `docker compose logs -f --since 1s worker | grep -E "job claimed|stage started|stage checkpointed|job resumed|retry started|job finished"`
3. Chrome: click **Take Notes** once; open the popup.
4. When terminal 1 shows `"stage started" … "EXTRACTING_FRAMES"`, note which container printed it (the
   log prefix, e.g. `worker-1`), then **immediately**:
   `docker kill notes-agent-worker-1` (use the container that printed the line). This sends SIGKILL.
5. Watch terminal 1: the other worker logs `job resumed from checkpoint` / `stage started
   EXTRACTING_FRAMES` for the same job ID, then PUBLISHING, then `job finished`.
6. Restore two workers: `docker compose up -d --no-deps --scale worker=2 worker`.

**Verify:**
- popup → Done → View in Notion opens the page;
- `jobs` row `COMPLETED` (or `COMPLETED_WITH_WARNINGS`), `attempts` 0 (a crash is not a counted
  failure);
- `job_events` shows two different `worker_id`s for the same job;
- exactly **one** new child page in Notion (count before/after);
- RabbitMQ `jobs.work.v2`: 0 messages, 0 unacked.

**Risks:**
- The frames stage can be short for short videos: the kill may land in another stage. That is still a
  valid crash demo; say which stage it hit.
- `docker kill` is a manual stop, so Docker may not restart the container itself; step 6 restores it.
- The survivor needs up to ~1 s to receive the redelivery: RabbitMQ sees the closed connection
  immediately.

**Cleanup:** step 6. Nothing else (the Notion page is the demo output).

**Rehearsal notes (2026-11-22):**
- On a 7-minute video, EXTRACTING_FRAMES started about 6 s after submission. A human cannot reliably
  hit that window by hand. The rehearsal used a small host-side watcher that follows
  `docker compose logs -f worker`, adopts the first job claimed, re-checks in the database that the job is
  in EXTRACTING_FRAMES, and then runs `docker kill` on the container named in that log line's prefix
  (`worker-N | …` → `notes-agent-worker-N`) only. For a manual demo, use a longer (~15 min) video, or
  kill at the `stage started` line of any stage.
- Evidence: `job_events` shows `resumed … {"redelivered": true, "delivery_count": 1}` from the
  surviving worker. The survivor's log has exactly one `POST https://api.notion.com/v1/pages`; the killed
  worker logged none.
- If the demo video was already published by an earlier job, Notion shows two pages with the same
  title. Only the one whose first block names **this** job ID belongs to the demo.
- The killed worker leaves an empty frame work directory (`/tmp/notesagent-frames/<pid>-<job>-…`). It is
  purged at the start of that worker's next frame stage (`purge_stale`, ADR-16).

---

## 2. Dead-letter queue

**Mechanism** (ADR-11/12):
- retryable failures are retried after the delays in `BACKOFF_SCHEDULE_SEC` (last step repeats) until
  `MAX_ATTEMPTS` total executions;
- then the job becomes `DEAD_LETTERED` (`error.code = RETRIES_EXHAUSTED`, `last_error_code` = the
  cause) and a copy goes to `jobs.dlq`;
- broker-level dead letters (a message that crashes workers more than `QUEUE_DELIVERY_LIMIT` times)
  are audited too.

**Fast demo profile** (bounded and configurable; spec §6). It needs no `.env` edit, because shell
variables override `.env` in Compose:

```bash
BACKOFF_SCHEDULE_SEC=2,4,8 MAX_ATTEMPTS=3 docker compose up -d gateway outbox-publisher worker
# revert afterwards (new shell, variables unset):
docker compose up -d gateway outbox-publisher worker
```

All three services declare the delay queues, so all three are recreated. New queues
`jobs.retry.{2,4,8}s` are declared and remain afterwards (empty, harmless). Revert with the second
command.

### 2-A Automated (ready)

```bash
docker compose --profile test run --rm --no-deps tests pytest -v -p no:warnings \
  tests/test_retries.py::test_backoff_schedule_last_step_repeats_then_dead_letter \
  tests/test_results_and_dlq.py::test_dead_lettered_job_state_after_exhausted_retries \
  tests/test_transcribe_stage.py::test_persistent_network_failure_is_bounded_and_dead_lettered \
  tests/test_summarize_stage.py::test_persistent_gemini_503_dead_letters_with_the_provider_status \
  tests/test_results_and_dlq.py::test_sigkill_redelivery_counts_toward_delivery_limit
```

**Expected:** the jobs end `DEAD_LETTERED` after exactly the configured attempts (delays 2, 4, …),
with the DLQ copy in `jobs.dlq`.

### 2-L Live (via the demo fault switch, ADR-22; makes **no external API call**)

**Demo fault switch (ADR-22):** enable it on the workers only, arm ONE video, then click Take Notes on
that video. The switch is off by default and an arming expires after 15 minutes. Only the first job of
the armed video is affected, together with its retries.

**Prerequisite (once; DONE 2026-11-22, workers on `78864607bddd`):** the workers must run an image that contains ADR-22. Build and deploy it to the
workers only: `docker compose build migrate`, then the enable command below. (Images built before
2026-11-22 do not contain the switch.)

```bash
DEMO_FAULTS_ENABLED=true docker compose up -d --no-deps --no-build --scale worker=2 worker   # recreates only the 2 workers
docker compose exec worker python -m notesagent.ops.demo_faults arm <scenario> --video <VIDEO_ID>
docker compose exec worker python -m notesagent.ops.demo_faults status    # armed / bound keys
# after the demos (variables unset in a new shell):
docker compose up -d --no-deps --no-build --scale worker=2 worker
```

1. Enable with a small budget:
   `DEMO_FAULTS_ENABLED=true MAX_ATTEMPTS=3 docker compose up -d --no-deps --no-build --scale worker=2 worker`. The default backoff
   gives 10 s + 30 s, about 40 s in total. No new delay queues are created.
2. Arm: `docker compose exec worker python -m notesagent.ops.demo_faults arm dlq --video <VIDEO_ID>`.
3. Take Notes on that video.
4. **The popup shows:** "Waiting to retry: YouTube could not be reached (attempt 1 of 3)", then "Failed
   after retries … Last cause: YouTube could not be reached."

**Verify:**
- `q "SELECT status, attempts, error FROM jobs ORDER BY created_at DESC LIMIT 1"` → `DEAD_LETTERED`,
  `attempts` 3, `RETRIES_EXHAUSTED` / `last_error_code` `YOUTUBE_UNREACHABLE`;
- `job_events`: 2 × `retry_scheduled` + `dead_lettered`;
- `rabbitmqctl list_queues -p / name messages` → `jobs.dlq` +1;
- worker logs: `docker compose logs worker | grep "demo fault injected"` → 3 lines.

**Why this is legitimate:**
- The fault replaces only the network call. Classification, retries and dead-lettering are the
  production code.
- YouTube is never contacted, so no external API is used.
- D-1 is unchanged: a private video still ends `FAILED` (show that too, if asked).

**Rehearsal notes (2026-11-22; job `129af1de`, video `favi7avxIag`):**
- **Timing:** submitted 22:27:26 UTC; `DEAD_LETTERED` 22:28:07, i.e. 41 s after the click.
- **Arming:** right after the click, `status` showed `demo:fault:job:<id> = dlq` and no armed video.
  Check this before waiting. If no binding appears, the job is running for real: stop.
- **Extra evidence that YouTube was never contacted:**
  - `video_title` and `duration_sec` stay NULL (only the worker sets them, from YouTube metadata);
  - 0 checkpoints;
  - 0 `HTTP Request` lines.
- **After the job:**
  - `disarm` prints "Nothing armed" because the job already claimed the arming;
  - the job-bound key `demo:fault:job:<id>` is left to expire (2 h TTL; inert with the switch off).
- **Disabling** restores `MAX_ATTEMPTS=5` and `DEMO_FAULTS_ENABLED=false`. Run it in a shell without
  those variables.
- **Check that it's off:** `arm` exits 2 with "REFUSED" (it refuses before touching Redis).

## 3. Graceful frame degradation

**Mechanism** (ADR-16/17):
- a timestamp whose frame cannot be extracted becomes a warning `FRAME_EXTRACTION_FAILED`;
- the page is published with the frames that succeeded, and the job is `COMPLETED_WITH_WARNINGS`;
- if the source video cannot be obtained, or every frame fails, a **text-only page** is published;
- the popup shows "Done, with warnings" and lists the omitted frames.

### 3-A Automated (ready)

```bash
docker compose --profile test run --rm --no-deps tests pytest -v -p no:warnings \
  tests/test_frames_stage.py::test_partial_frame_failure_completes_with_warnings \
  tests/test_frames_stage.py::test_all_frames_failing_continues_text_only \
  tests/test_publish_stage.py::test_partial_frames_complete_with_warnings \
  tests/test_publish_stage.py::test_text_only_page_when_no_frame_could_be_captured \
  tests/test_results_and_dlq.py::test_partial_frames_complete_with_warnings
cd extension && npm test     # includes the popup's "Done, with warnings" rendering
```

### 3-L Live (via the demo fault switch, ADR-22; 1 real job each)

**Demo fault switch (ADR-22):** enable it on the workers only, arm ONE video, then click Take Notes on
that video. The switch is off by default and an arming expires after 15 minutes. Only the first job of
the armed video is affected, together with its retries.

**Prerequisite (once; DONE 2026-11-22, workers on `78864607bddd`):** the workers must run an image that contains ADR-22. Build and deploy it to the
workers only: `docker compose build migrate`, then the enable command below. (Images built before
2026-11-22 do not contain the switch.)

```bash
DEMO_FAULTS_ENABLED=true docker compose up -d --no-deps --no-build --scale worker=2 worker   # recreates only the 2 workers
docker compose exec worker python -m notesagent.ops.demo_faults arm <scenario> --video <VIDEO_ID>
docker compose exec worker python -m notesagent.ops.demo_faults status    # armed / bound keys
# after the demos (variables unset in a new shell):
docker compose up -d --no-deps --no-build --scale worker=2 worker
```

1. **Partial:** arm `frames_partial`, then Take Notes. Every second frame (index 1, 3, …) fails; frame 0
   is always real.
   - **Expected:** popup "Done, with warnings" listing the omitted frames; `jobs.status`
     `COMPLETED_WITH_WARNINGS`; warnings `FRAME_EXTRACTION_FAILED` with reason `DEMO_INJECTED_FAILURE`;
     `result.frames_published < frames_requested`; the Notion page has all the text plus the remaining
     real frames.
2. **All frames (spec: "if every frame fails, publish a text-only page"):** arm `frames_all`, then Take
   Notes.
   - **Expected:** `COMPLETED_WITH_WARNINGS`, `frames_published` 0, and a **text-only** Notion page.

Use a video with at least 2 visual moments (normal for a 5–10-minute lecture).

## 4. Worker scaling (spec §8)

### 4-A Automated (ready)

```bash
docker compose --profile test run --rm --no-deps tests pytest -v -p no:warnings \
  tests/test_concurrency.py::test_two_workers_never_process_the_same_job_simultaneously
```

### 4-L Live (ready; scaling itself needs no job)

> **Always name the service and use `--no-deps`** (found in the 2026-11-22 rehearsal dry run). A bare
> `docker compose up -d --scale worker=N` reconciles the whole project. If `notes-agent-backend:dev`
> differs from the image the gateway and publisher run (true after the worker-only Flash-Lite
> deployment), it **recreates the gateway, the outbox publisher and migrate**. Run
> `docker compose --dry-run …` first when unsure.


```bash
docker compose up -d --no-deps --scale worker=3 worker   # creates only worker-3
docker compose ps worker                                   # 3 x healthy
curl -s http://localhost:8000/healthz                      # "live_workers":3, "work_queue_consumers":3
docker exec notes-agent-rabbitmq-1 rabbitmqctl list_queues -p / name consumers   # jobs.work.v2 3
docker compose up -d --no-deps --scale worker=2 worker    # scale down (removes worker-3 only)
docker compose logs --since 1m worker | grep "worker stopping"
curl -s http://localhost:8000/healthz                      # back to 2
```

**Optional, with approval:** click Take Notes on 2–3 different videos while 3 workers run; the
`job_events.worker_id` values show the jobs spread across workers.

**Risks:**
- Scaling down sends SIGTERM to a worker. If it holds a job, the job is not acknowledged and is
  redelivered after the 15 s grace period at the latest. That is safe: the crash-recovery path.
- Each worker can use up to 768 MB of memory.
- `live_workers` returned to 2 about 3 s after a graceful scale-down (observed 2026-11-22). After a SIGKILL it can take up to the heartbeat TTL (15 s).

**Cleanup:** `docker compose up -d --no-deps --scale worker=2 worker`.

---

## 5. HTTP 429 / rate limits (all three APIs)

**Mechanism** (ADR-10/11/13):
- **Prevention:** a shared Redis limiter is taken before every external call:
  - Gemini: requests per minute, input tokens per minute and requests per Pacific day;
  - Notion: requests per second per connection;
  - YouTube: cooldown only.
- **On a real 429:** the response is classified as `RateLimited`.
  - The Retry-After / `RetryInfo` delay becomes a **shared cooldown for all workers**.
  - The job is re-queued with `max(backoff step, Retry-After)` through the delay queues.
  - It stays in its current stage with a visible `retry` object in `GET /jobs/{id}`; the popup shows
    "Waiting to retry: <provider> is limiting requests (attempt n of m). Still processing.".
  - After `MAX_ATTEMPTS` it dead-letters.
- A Gemini daily quota 429 waits for the reset (`resets_at`).

### 5-A Automated (ready; one per provider)

```bash
docker compose --profile test run --rm --no-deps tests pytest -v -p no:warnings \
  tests/test_transcribe_stage.py::test_youtube_429_uses_shared_cooldown_and_the_existing_retry \
  tests/test_frames_stage.py::test_download_429_is_retried_through_the_outbox \
  tests/test_summarize_stage.py::test_gemini_429_retry_info_is_honoured_and_shared \
  tests/test_publish_stage.py::test_notion_429_is_retried_and_publishes_once \
  tests/test_ratelimit.py::test_provider_429_in_one_worker_cools_down_the_others \
  tests/test_retries.py::test_rate_limited_retry_after_is_honoured_with_uncounted_hops
cd extension && npm test     # "Waiting to retry" popup wording
```

**Expected:** each job shows a `retry` entry (reason `*_RATE_LIMITED`, delay ≥ Retry-After), the cooldown
key is set, and the job then completes. For Notion: exactly one page.

### 5-L Live (via the demo fault switch, ADR-22; 1 real job)

**Demo fault switch (ADR-22):** enable it on the workers only, arm ONE video, then click Take Notes on
that video. The switch is off by default and an arming expires after 15 minutes. Only the first job of
the armed video is affected, together with its retries.

**Prerequisite (once; DONE 2026-11-22, workers on `78864607bddd`):** the workers must run an image that contains ADR-22. Build and deploy it to the
workers only: `docker compose build migrate`, then the enable command below. (Images built before
2026-11-22 do not contain the switch.)

```bash
DEMO_FAULTS_ENABLED=true docker compose up -d --no-deps --no-build --scale worker=2 worker   # recreates only the 2 workers
docker compose exec worker python -m notesagent.ops.demo_faults arm <scenario> --video <VIDEO_ID>
docker compose exec worker python -m notesagent.ops.demo_faults status    # armed / bound keys
# after the demos (variables unset in a new shell):
docker compose up -d --no-deps --no-build --scale worker=2 worker
```

1. Arm `gemini_429_once`, then Take Notes.
2. **Expected:**
   - the SUMMARIZING attempt receives an injected Gemini 429 (`RESOURCE_EXHAUSTED`, Retry-After 20 s)
     **instead of** a request, so Google is not contacted for it;
   - the job stays in SUMMARIZING, and the popup shows "Waiting to retry: the AI model is limiting
     requests (attempt 1 of 5). Still processing.";
   - `GET /jobs/{id}` → `retry.reason` `GEMINI_RATE_LIMITED`;
   - Redis `ratelimit:cooldown:gemini` is set (about 20 s; it also delays other workers' Gemini calls);
   - after about 20 s the next attempt passes the limiter, makes **the one real Gemini request**, and
     the job completes.
3. **Verify:**
   - `job_events` `retry_scheduled` with `http_status` 429, `provider_code` `RESOURCE_EXHAUSTED`,
     `delay_sec` ≥ 20;
   - final `COMPLETED`, `attempts` 1;
   - worker log `demo fault injected` × 1.

**Rehearsal notes (2026-11-22; job `6649512b`, video `fyTxwIa-1U0`):**
- **Capture the waiting state within about 20 s** of the click, or it's gone:
  - `ratelimit:cooldown:gemini` `PTTL` (≈ 19 s observed);
  - `GET /jobs/{id}` → `retry.reason GEMINI_RATE_LIMITED`.
- **Proof that the simulated 429 made no Google call:** the worker HTTP log lines. There is no
  `generateContent` line at the injection time; the first one comes after the 20 s retry.
- **A real Gemini 503 can follow the injected 429** (it happened here). The job then waits the ADR-21
  backoff (60 s, then 180 s, …) and still completes. It costs one extra limiter permit, so budget ≥ 3
  free permits.

**Scope note:**
- Only the Gemini 429 is injected live.
- YouTube 429 and Notion 429 handling use the same `RateLimited` → cooldown → backoff path. Show them
  with 5-A (automated, real infrastructure, fake providers).

## 6. Implementation status of the former gaps

Closed by ADR-22 (implemented 2026-11-22, tests in `backend/tests/test_unit_demo_faults.py` and
`backend/tests/test_demo_faults.py`): the live 429, frame-failure and DLQ triggers. D-1 is unchanged.
**All live procedures were rehearsed successfully on 2026-11-22:**
- 2-L DLQ (`dlq_demo_rehearsal_success.md`);
- 3-L `frames_partial` and `frames_all` (`frames_partial_demo.md`, `frames_all_demo.md`);
- 5-L Gemini 429 (`gemini_429_once_demo.md`).

Nothing remains to rehearse.

## 7. Suggested demo order (≈ 20 min)

1. `/healthz`, `docker compose ps`.
2. Scaling 4-L (no job).
3. Crash 1-L (1 job).
4. DLQ (2-L), degradation (3-L) and Gemini 429 (5-L) live via the demo fault switch; YouTube/Notion
   429 via 5-A.
5. Disable the switch (`docker compose up -d --no-deps --no-build --scale worker=2 worker`), revert any demo profile, and keep
   2 workers.
