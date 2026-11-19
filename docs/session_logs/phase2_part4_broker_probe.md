# Session log — Phase 2, Part 4: RabbitMQ broker behaviour probe

- **Date:** 2026-11-19
- **Branch:** `phase-2`; HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged; `git status` 41 entries
  (the uncommitted Phase 2 work, unchanged)
- **Nature:** isolated broker investigation. No application code, tests, migrations, Docker / RabbitMQ
  configuration, project topology or existing documentation changed. This log is the only project file
  written. Part 4 **not** started; no migration 004.
- Earlier logs (unchanged), incl. `phase2_part4_requirements_review.md`.

## 1. Objective
Measure on the running RabbitMQ the behaviour that decisions P4-D1 (death-reason parsing), P4-D4
(lock-busy deferral), P4-D5 (audit queue delivery limit) and P4-D7 (PostgreSQL-outage deferral) rely on.

## 2. Initial environment (inspected at runtime)
| Item | Value |
|---|---|
| Container | `notes-agent-rabbitmq-1`, image `rabbitmq:4.4.1-management`, digest `sha256:61f90c40…1677`, healthy, restarts 0, started 2026-11-19T08:27:49Z |
| `rabbitmq-diagnostics server_version` | **4.4.1** (expected version — probe proceeds) |
| Alarms | none |
| Vhosts | `/` (dev stack), `notes_test` (test suite) |
| Users | `notes` [administrator] |
| Policies / operator policies | none in `/`; none in `notes_test` |

**Dev vhost `/` (still Phase 1 — not migrated):** `jobs` (direct) → `jobs.work` (quorum,
`x-dead-letter-exchange=jobs.dlx`, `x-delivery-limit=10`; no dead-letter strategy → at-most-once; 2
consumers = dev workers); `jobs.dlx` (fanout) → `jobs.dlq` (quorum). No `jobs.retry`, no delay queues.

**Test vhost `notes_test` (v2, test profile):** `jobs` → `jobs.work.v2` (quorum, `x-delivery-limit=10`,
DLX `jobs.dlx`, `x-dead-letter-strategy=at-least-once`, `x-overflow=reject-publish`); `jobs.retry`
(direct) → `jobs.retry.{2,4,5,8,30}s` (quorum, `x-message-ttl`, DLX `jobs`/`job.work`, at-least-once,
reject-publish); `jobs.dlx` (fanout) → `jobs.dlq` (quorum). All queues empty.

## 3. Method and isolation
- A **temporary vhost** is created for the probe, with permissions for user `notes`. All probe
  exchanges/queues live only there; nothing in `/` or `notes_test` is declared, bound, purged or deleted.
  The vhost is deleted at the end.
- The probe script lives in the scratchpad (outside the repository) and runs inside a throw-away
  `tests` container (`docker compose --profile test run --rm --no-deps -T tests python -`), which has
  aio-pika 10.2.3 — the client the workers use. It reads the broker URL from the container environment
  and never prints credentials.
- A consumer **crash** is a real child process that receives the message without acking and is then
  killed with **SIGKILL**.
- Probe queues use `x-delivery-limit = 3` (small test limit) unless stated otherwise.

## 4. Temporary resources
- Vhost **`p4probe-1795087585`** was created at 16:56 IST (`rabbitmqctl add_vhost` + `set_permissions
  notes .* .* .*`) and deleted at the end (`rabbitmqctl delete_vhost`). All probe exchanges and queues
  were inside it:
  - exchanges `px.jobs` (direct), `px.retry` (direct), `px.dlx` (fanout);
  - `px.work`, mirroring the `jobs.work.v2` arguments with limit 3;
  - `px.delay.2s`, mirroring a delay queue with a 2 s TTL;
  - `px.dlq`, and `px.audit` in the proposed D5 shape;
  - `px.b1…b4` (with `.dlx`/`.dlq`), `px.c` (with `.dlx`/`.dlq`), `px.d1…d6`, `px.e1`, `px.e2`,
    `px.e2.park`.
- After deletion, `list_vhosts` shows only `/` and `notes_test`:
  - `/`: `jobs.work` 0 messages / 2 consumers, `jobs.dlq` 0;
  - `notes_test`: all queues 0 messages;
  - RabbitMQ healthy, 0 restarts, no alarms.
- Final state of the probe queues just before deletion (supporting evidence):
  - **messages still present:** px.b1, b2, b4, d1, d2, e1 (nacked), px.d4 (limit −1), px.d6
    (republished), px.e2.park (parked);
  - **empty:** px.d3, px.d5 (dropped), px.b3, px.c, px.work, px.e2 (dead-lettered).
- Scripts and raw outputs are in the scratchpad, outside the repo: `broker_probe.py`,
  `broker_probe_e.py` (+ the assembled `broker_probe_e_full.py`), `probe_output.jsonl` (155 lines),
  `probe_output_e.jsonl`.
- Harness issues (not results):
  - Run 1 failed immediately with `KeyError: 'RABBITMQ_URL'`, because the tests container names the
    variable `TEST_RABBITMQ_URL`. Fixed with a fallback in the script.
  - The first two attempts to assemble probe E failed in the shell before running anything.
  - stderr of the main run contains only aio-pika shutdown warnings ("Task was destroyed but it is
    pending").

## 5. Commands
```
docker exec notes-agent-rabbitmq-1 rabbitmq-diagnostics -q server_version        # 4.4.1
docker exec notes-agent-rabbitmq-1 rabbitmqctl -q list_queues|list_exchanges|list_bindings|list_policies -p <vhost> ...
docker exec notes-agent-rabbitmq-1 rabbitmqctl -q add_vhost p4probe-1795087585 ; set_permissions ...
MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps -T -e PROBE_VHOST=p4probe-1795087585 tests python - < broker_probe.py
... same for broker_probe_e_full.py
docker exec notes-agent-rabbitmq-1 rabbitmqctl -q delete_vhost p4probe-1795087585
```

## 6. Results — verified runtime observations (RabbitMQ 4.4.1, aio-pika 10.2.3)

### Experiment A — death headers
Dead-letter copies were read from `px.dlq`. The `px.audit` copy had identical headers every time.

| Case | `x-death` (as delivered, in order) | first-death reason / queue | last-death reason / queue | `x-delivery-count` |
|---|---|---|---|---|
| A1 rejected (`reject(requeue=False)`) | [rejected @ px.work] | rejected / px.work | rejected / px.work | 1 |
| A2 expired in delay queue, delivered from work queue | [expired @ px.delay.2s] | expired / px.delay.2s | expired / px.delay.2s | absent |
| A3 expired → work queue → delivery_limit (4 SIGKILLed deliveries) | [**delivery_limit @ px.work** (11:26:50), expired @ px.delay.2s (11:26:47)] | **expired / px.delay.2s** | **delivery_limit / px.work** | 4 |
| A4 Part 3 republish filter (copied verbatim from `broker.republish`) → delay → work → delivery_limit | [delivery_limit @ px.work, expired @ px.delay.2s (the post-republish expiry only)] | expired / px.delay.2s | delivery_limit / px.work | 4 |
| A5 delivery_limit only | [delivery_limit @ px.work] | delivery_limit / px.work | delivery_limit / px.work | 4 |

Other headers present: `x-first/last-death-exchange`, `x-acquired-count` (the number of acquisitions),
and our own `x-job-id` and `x-infra-delays`, which were preserved. The AMQP `message_id` was preserved in
every case (A1–A5), and `redelivered` was true on the dead-lettered copies.

- `x-death` is ordered **newest first** (A3/A4: 11:26:50 before 11:26:47).
- The work-queue death can be told apart from the delay-queue expiry by its `queue` and `reason`.
- `x-first-death-reason` is **wrong** for a retried message (it says `expired`).
  `x-last-death-reason` / `x-last-death-queue` give the work-queue death.
- The Part 3 republish filter strips the old history, and the copy then gets a fresh expiry entry from
  the delay queue. The final dead letter still carries the correct last death.
- With `x-delivery-limit = 3`, the message was delivered **4 times** (counts seen: absent, 1, 2, 3). It
  was dead-lettered when the consumer of the 4th delivery crashed (`x-delivery-count` 4 on the copy).

### Experiment B — explicit requeue (limit 3)
| Queue | Operation | Deliveries | `x-delivery-count` seen | Outcome |
|---|---|---|---|---|
| px.b1 (DLX) | `basic.get` + **`nack(requeue=True)`** | 10 | absent every time | still in queue, DLQ 0 |
| px.b2 (DLX) | `basic.consume` + **`nack(requeue=True)`** | 10 | absent every time | still in queue, DLQ 0 |
| px.b3 (DLX) | `basic.get` + **`reject(requeue=True)`** | 4 | absent, 1, 2, 3 | **dead-lettered**: last-death `delivery_limit` / px.b3, count 4 |
| px.b4 (no DLX) | `basic.get` + `nack(requeue=True)` | 10 | absent | still in queue |
| px.e1 | `nack(requeue=True)`, full headers | 4 | `x-delivery-count` absent; `x-acquired-count` 1, 2, 3 | still in queue |

**`basic.nack(requeue=True)` is not counted; `basic.reject(requeue=True)` is counted.** `x-acquired-count`
counts every acquisition, including nacked ones, but it does not drive the limit.

### Experiment C — consumer crash (SIGKILL before ack), limit 3
Counts seen: absent, 1, 2, 3. The message was dead-lettered on the 4th crash; the DLQ copy has
last-death `delivery_limit` / px.c and `x-delivery-count` 4. **Crashes are counted**, unlike
`nack(requeue=True)`.

### Experiment D — audit-queue retry safety (quorum, no DLX unless stated)
| Case | Result |
|---|---|
| D1 limit 3, 10× `nack(requeue)` (lock-contention style) | never counted; message kept |
| D2 limit 3, 6× throttled consume + `nack(requeue)` (outage style, 1 s) | never counted; message kept |
| D3 limit 3, SIGKILL loop | 4 deliveries, then the message **disappeared** (queue 0, no DLX): discarded |
| D4 `x-delivery-limit = -1` | declaration accepted; 22 SIGKILLs (count up to 21); message **still present** |
| D5 no `x-delivery-limit` argument | dropped after 21 deliveries (last count 20) → **default limit 20** |
| D6 limit 3: after 2 crashes, confirmed copy (same `message_id`) + ack of the original | the copy starts with no `x-delivery-count`, so the counter is reset; message kept |
| E2 limit 3 + `x-dead-letter-exchange=""`, `x-dead-letter-routing-key=px.e2.park`, at-least-once, reject-publish | 5 nacks did not move it; then 4 SIGKILLs put a copy in `px.e2.park` (last-death `delivery_limit` / px.e2). **Parked, not lost.** |

Note on E2: the parking death overwrote the `x-first/last-death-*` headers, and `x-death` gained a px.e2
entry. In E2 the original work-queue death was simulated with headers only.

## 7. Documentation-supported behaviour (not re-verified here)
- RabbitMQ 4.0 introduced a default quorum-queue delivery limit of 20 (D5 matches this).
- `-1` disables the limit (D4 matches this).
- **Not verified:** why `basic.reject` and `basic.nack` differ in counting. Only the behaviour was
  observed.

## 8. Answers to the required questions
1. **Can P4-D1 reliably select the most recent death from `jobs.work.v2`?** Yes [runtime]: use
   `x-last-death-queue` / `x-last-death-reason`, cross-checked with `x-death[0]` (newest first).
2. **Header parsing logic (recommendation):**
   - Take `x-death[0]` (the newest entry).
   - Require `queue ∈ {jobs.work.v2, jobs.work}` and `reason ∈ {delivery_limit, rejected}`.
   - Confirm it equals `x-last-death-queue` / `x-last-death-reason`.
   - If anything is missing or inconsistent, set `death_reason = unknown`, record it, and do not
     dead-letter the job.
   - Never use `x-first-death-*`.
   - No `x-death` and a body with `event_type = job.dead_lettered` means a worker-initiated copy (P4-D2).
3. **Does `nack(requeue=True)` increment the delivery count?** **No** [runtime: B1, B2, D1, D2, E1].
   `reject(requeue=True)` **does** [runtime: B3].
4. **Does a consumer crash increment it?** **Yes** [runtime: A3–A5, C, D3].
5. **Can D4/D7 exhaust the audit queue's limit?** Not through the nack itself [runtime], provided the
   implementation uses `nack` and never `reject(requeue=True)`. Worker crashes (SIGKILL, OOM, container
   restarts) while an audit message is unacked **are** counted, though. Repeated crashes during a long
   deferral or outage can therefore exhaust the limit [runtime C/D3; the scenario itself is inferred].
6. **Is D5's finite limit with no DLX safe?** **No** [runtime D3]. At the limit the message is discarded
   silently, so a legitimate audit message can be lost after enough crashes.
7. **What prevents silent loss (recommendation):**
   - **(a, recommended)** A finite limit **plus** a dead-letter target to a separate parking queue (e.g.
     `jobs.dlq.audit.parked` via the default exchange, at-least-once + reject-publish), verified in E2.
     Temporary failures use `nack(requeue)`, which is not counted; only real crash loops park the message
     instead of dropping it. Caveat: parking overwrites the `x-last-death-*` headers. After parking, the
     original work-queue death is available only from the older `x-death` entries, so the parking queue
     is for manual inspection.
   - (b) `x-delivery-limit = -1`. Nothing is lost, but a poison audit message crashes workers forever.
   - (c) Republish-then-ack (D6) also resets the count, but adds a publish per deferral.
   - Do **not** omit the argument: the default limit of 20 applies (D5), and without a DLX the message
     is dropped.
8. **Audit message at its limit with no DLX:** discarded (D3, D5).
9. **Decisions to revise:**
   - **P4-D1:** confirmed; use the last death / `x-death[0]`.
   - **P4-D5:** no DLX is unsafe; use a parking queue instead.
   - **P4-D4 / P4-D7:** keep `nack(requeue)`, but state explicitly that `reject(requeue=True)` must
     never be used and that crashes still count.
   - The other decisions are unaffected by the broker evidence; the P4-D2 detection rule is now concrete.

## 9. Related observation on existing code (no change made)
`grep '.(reject|nack)('` in `backend/`:
- Requeues use `nack(requeue=True)`: `broker.py:270`, `runner.py:241` and the test helpers.
- `reject` is used only with `requeue=False` (`runner.py:212`).

This is consistent with the measured semantics. ADR-11's "not counted" statement holds **only for
`nack`**.

## 10. Unresolved
- The mechanism behind the reject-versus-nack counting difference (observed only).
- The header layout after a *real* two-stage dead letter into a parking queue (E2 simulated the first
  death with headers only).
- Behaviour across broker restarts or leader changes (single node; not tested).

## 11. Git and files
- Before and after: HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged; `git status` 41 entries.
- The only project file written is this log (`docs/session_logs/phase2_part4_broker_probe.md`, new).
- Not modified: application code, tests, migrations, Compose, RabbitMQ config, project topology,
  earlier logs. Part 4 not started; no migration 004.
