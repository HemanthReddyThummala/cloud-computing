# Status document cleanup after the live failure demonstrations (docs only)

Date: 2026-11-22 IST. The user approved a documentation-only cleanup of stale status rows.

Not done:
- no code, configuration, image, container, Redis, database or external API change;
- no job; no demo;
- no secrets read;
- no Git operation;
- root `handover.md` untouched.

## Rationale

The §3 feature table and §8.1 in `docs/PROJECT_STATUS_AND_HANDOVER.md` were written before the
2026-11-22 live demos. Several entries were demonstrably stale. Every correction below is backed by an
existing session log or by a read-only database check made in this session. Nothing unverified was
promoted to verified.

## Changes

### `docs/PROJECT_STATUS_AND_HANDOVER.md`

| Location | Before | After | Evidence |
|---|---|---|---|
| Header "Last updated" | an awkward two-part line from the previous update (unbalanced parenthesis) | one clean line: docs cleanup; switch off; ADR-23 deployed with an empty namespace | — |
| §3 Retries / DLQ | "3 real jobs … `jobs.dlq` = 3 messages" | **5** real jobs / **`jobs.dlq` = 5**, listed by last cause (`acffab62`, `f87c737a`: `GEMINI_HTTP_503`; `82960179`, `44df00c0`: `GEMINI_DAILY_LIMIT_REACHED`; `129af1de`: `YOUTUBE_UNREACHABLE`, the DLQ demo) | `SELECT … WHERE status='DEAD_LETTERED'` (read-only, this session) → exactly those 5; queue count 5 in `gemini_429_once_demo.md` §6 |
| §3 Graceful degradation | "🧪 Tested (fakes) only" | ✅ verified live + 🧪: partial **4/7** published (job `82c49d33`), all-fail **0/4**, **text-only page** (job `74779edd`) | `frames_partial_demo.md` §5, `frames_all_demo.md` §5 |
| §3 Failure demonstrations | "DLQ / frame degradation / Gemini 429 … **not deployed, not rehearsed**" | all six deployed and rehearsed live: scaling, crash recovery, DLQ, partial frames, all frames, Gemini 429. The switch is deployed and off. Per-demo evidence and the session-log names were added; the original crash, scaling and ADR-22 text is kept as "Earlier evidence"; YouTube/Notion 429 explicitly remain tests (5-A) only; the real 503 during the 429 demo is noted | `scaling_rehearsal_success.md`, `crash_recovery_rehearsal_success.md`, `dlq_demo_rehearsal_success.md`, `frames_partial_demo.md`, `frames_all_demo.md`, `gemini_429_once_demo.md` |
| §8.1 End-to-end | three Unverified items: merge path; no-duplicate under a crash; `COMPLETED_WITH_WARNINGS` | **Live-verified:** `COMPLETED_WITH_WARNINGS` (2 jobs); no duplicate page under crash recovery **for a crash during EXTRACTING_FRAMES** (job `6141336d`). **Tests only:** a crash during PUBLISHING. **Still Unverified:** the merge path, and burst against real Gemini (the unplanned 4-job burst is mentioned as an uncontrolled observation, not as verification). The Flash-Lite safety-margin paragraph is unchanged | `crash_recovery_rehearsal_success.md`; the two frame-demo logs; `job_44df00c0_origin_investigation.md` |
| §10 history | — | a new row for this log | — |

Diff size: 10 lines removed, 18 added. All removed lines were stale statements or the reformatted
header; no historical details were deleted. The history rows that say "not deployed" or "not rehearsed"
were accurate at their time and are kept.

### `docs/FAILURE_DEMONSTRATION_RUNBOOK.md`

| Location | Before | After |
|---|---|---|
| §6 "Implementation status of the former gaps" | "Remaining: rehearse 3-L and 5-L once with the user (3 real jobs …)" | all live procedures (2-L, 3-L both scenarios, 5-L) rehearsed successfully on 2026-11-22, with log names; nothing remains to rehearse |

The readiness table (rows 1–5) and the per-demo rehearsal notes were already current; no change.

## Consistency check

- **Searched both documents** for `not rehearsed`, `not deployed`, old `jobs.dlq` counts,
  `COMPLETED_WITH_WARNINGS … Unverified` and `rehearse 2-L/3-L/5-L`. After the edits, only historical
  §10 rows remain (correct for their date).
- **Unchanged and still accurate:**
  - §3 "Shared rate limiter": burst **Unverified**;
  - "Latency near the 20-min cap": ⬜ not measured;
  - "Popup wording for Gemini 503/504": not seen in Chrome;
  - §9 item 3 (R9 evaluation items).
- **Not changed:** `README.md` (no stale demo-status claims).
