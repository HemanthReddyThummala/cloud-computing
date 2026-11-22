# Failure demonstration runbook preparation (spec §6)

Date: 2026-11-22 IST. Scope: prepare `docs/FAILURE_DEMONSTRATION_RUNBOOK.md`; **no demo executed**.

Not done (by instruction):
- no real YouTube, Gemini or Notion call;
- no job;
- no container killed or restarted;
- no application-code, `.env` or Git change.
- Root `handover.md` untouched.

## 1. Investigated

- **Spec §6** (verbatim): the four failure modes must be handled "demonstrably, live":
  - 429 from **any** of the three APIs, re-queued with backoff and visibly waiting;
  - worker SIGKILL mid-job → reassigned, no loss, no duplicate Notion page;
  - graceful degradation → `COMPLETED_WITH_WARNINGS` / text-only;
  - DLQ after a bounded, configurable number of attempts ("submitting a private/unavailable video is
    a natural way to trigger this").
- **Spec §8:** scaling with `--scale worker=N`.
- **ADRs:**
  - 05/11 (ack after commit, retries via the outbox, uncounted hops);
  - 10/12 (classification, broker dead-letter audit);
  - 13 (limiter, shared cooldown);
  - 14 (**decision D-1:** a private / unavailable video → `FAILED`, "the DLQ is demonstrated with
    retryable failures instead");
  - 16/17 (warnings, text-only, reconciliation).
- **Code:**
  - `runtime.py`: SIGTERM/SIGINT → stop.
  - `runner.py`: consumer cancelled on stop; log messages `job claimed`, `stage started`,
    `stage checkpointed`, `job resumed from checkpoint`, `retry started`, `job finished`,
    `worker stopping` (all confirmed present).
  - `broker.py`: every service declares the delay queues from `delay_queue_seconds`.
  - `ratelimit.py`: `LOCAL_CODES`; the local daily limit raises `QuotaExhausted`
    `GEMINI_DAILY_LIMIT_REACHED`; `GEMINI_LOCAL_RATE_LIMIT`.
  - `frames.py`: `FRAME_EXTRACTION_FAILED` reasons.
  - Config defaults: heartbeat TTL 15, infra 20 × 30 s, `RATE_LIMIT_MAX_WAIT_SEC` 60.
- **Fault injection:** the production code has **none**. Test fakes and fixture stages exist only in the
  test image. `CHAOS_ENABLED` appears only in `.env.example` (unused).
- **Compose:** `restart: unless-stopped`; worker `stop_grace_period: 15s`; replicas
  `${WORKER_REPLICAS:-2}`; worker memory limit 768 MB.
- **Tests:** inventory of crash / DLQ / degradation / 429 / concurrency tests; 22 selected for the
  runbook.

## 2. Results

| Demo | Automated (real infra, fake providers) | Live |
|---|---|---|
| Worker crash recovery | ready (5 tests) | **ready**: Take Notes on a ~10-min video, `docker kill` the worker logging EXTRACTING_FRAMES, verify via logs / `job_events` / one Notion page; 1 real job |
| DLQ | ready (5 tests, including the broker delivery limit) | **gap**: the spec's private-video trigger ends `FAILED` (D-1); retryable failures cannot be produced live. Options: revisit D-1; Redis-stop infra-budget demo (possible, not rehearsed, invasive); fault switch |
| Graceful degradation | ready (5 tests + extension popup tests) | **gap**: no way to force frame failure on a real video |
| Scaling | ready (concurrency test) | **ready**: `--scale worker=3/2`, verified via `/healthz` and consumers; no job needed |
| 429 (all 3 APIs) | ready (6 tests: YouTube, download, Gemini, Notion, cross-worker cooldown, Retry-After hops) | **gap**: a real 429 cannot be triggered safely. The `GEMINI_RPM_LIMIT=1` burst shows proactive limiting only (2 real jobs) |

- **Fast demo profile without editing `.env`:** shell variables override `.env` in Compose
  (`BACKOFF_SCHEDULE_SEC=2,4,8 MAX_ATTEMPTS=3 docker compose up -d gateway outbox-publisher worker`).
- **Proposed smallest fix (not implemented):** one env-gated, default-off `DEMO_FAULT` switch raising the
  real classified errors at the provider boundary. It would close the 429, frame-failure and DLQ gaps
  and make them visible in the popup.

## 3. Commands executed (all read-only or test collection)

| Command | Result |
|---|---|
| greps of `PROJECT_SPEC` text, ADRs, `runner.py`, `runtime.py`, `broker.py`, `ratelimit.py`, `frames.py`, `config.py`, `docker-compose.yml`, tests | facts in §1 |
| `docker compose --profile test run --rm --no-deps tests pytest --collect-only -q <22 test ids>` | **22 tests collected** (every cited test exists; nothing executed) |
| evidence-query helper `q()` against the running database (2 read-only SELECTs) | works; the runbook uses it |

## 4. Files changed

| File | Why |
|---|---|
| `docs/FAILURE_DEMONSTRATION_RUNBOOK.md` (new) | the deliverable: per demo, the mechanism, automated commands, live steps, expected results, verification, cleanup, risks; readiness table; gaps and the proposed fix; demo order |
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | failure-demo row, remaining work (decision needed), date, history |
| `docs/session_logs/failure_demo_runbook_preparation.md` (new) | this log |

## 5. Unresolved

- **User decision:** the demo fault switch and/or revisiting D-1, which determines whether DLQ, 429 and
  degradation can be shown live.
- **No live demo has been rehearsed.** The automated tests cited were not re-run in this session (last
  full run: all 902 passed, 2026-11-22).
- Live crash timing depends on the EXTRACTING_FRAMES duration of the chosen video.

## 6. Recommended next stage

Implement the single default-off demo fault switch (and decide D-1), with tests and README / runbook
updates. Then rehearse all live demos once with the user's approval (2–4 real jobs).
