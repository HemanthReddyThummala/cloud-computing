# Real end-to-end test attempt — blocked by the daily Gemini quota (no external calls)

Date: 2026-11-21 04:39–04:4x IST (2026-11-20 23:09 UTC).

Task: inspect the stack, run one controlled Gemini probe, then (only on success) one real Take Notes
job, fix any issues, and document.

**Outcome:** Phases 1–2 were completed. **Phase 3 (the probe) was not run**: our own rate limiter
still counts **16/16** Gemini requests for Pacific day 2026-11-20, so any request would be refused
locally before reaching Google. A local refusal is not a Gemini response and proves nothing about
Gemini, and bypassing the limiter could exceed Google's real free-tier daily limit. Phases 4–6 therefore
did not start. No code was changed.

## 1. Initial state

- Branch `phase-2`, **HEAD `13a9334` "ADR-21"** (committed by the user at 04:32 IST); working tree
  clean (only untracked files from earlier sessions).
- `git diff --quiet 13a9334 -- backend extension docker-compose.yml` → identical.

## 2. Phase 1 inspection (all read-only, apart from one image tag)

| Check | Result |
|---|---|
| Containers | gateway, outbox-publisher, worker ×2 on `2a01b00b9603`, healthy, 0 restarts (since 22:56 UTC); postgres / rabbitmq / redis healthy, 0 restarts (since 2026-11-19); migrate exited 0 |
| Deployed vs committed | all 48 backend files of the image are **byte-identical** to `git show 13a9334:backend/…`. Added the tag `notes-agent-backend:13a9334` → `2a01b00b9603` (an alias; nothing restarted) |
| `/healthz` | 200; postgres ok (outbox_pending 0); rabbitmq ok (`jobs.work.v2` 0 messages, 2 consumers); redis ok (2 live workers) |
| RabbitMQ | `jobs.work.v2` 0 / 2 consumers; `jobs.dlq.audit` 0 / 2; retry queues 0; `jobs.dlq` 3 (the known dead-letter copies) |
| Jobs | 5: 2 early FAILED (stage not available at the time), 3 DEAD_LETTERED at SUMMARIZING (`D26sUZ6DHNQ` ×2 GEMINI_HTTP_503, `_S-cBzAg0GE` GEMINI_DAILY_LIMIT_REACHED). None in flight |
| Redis limiter | `ratelimit:gemini:rpd:2026-11-20` = **16** (limit 16); the Pacific day ends at 08:00 UTC = **13:30 IST** on 2026-11-21 |
| Worker configuration (presence only) | `NOTION_DEFAULT_PARENT_ID` parsed; Gemini key set; model `gemini-3.8-flash` |
| Extension | `extension/config.js` → `http://localhost:8000`; `/livez` 200; `POST /oauth/notion/callback` with an unknown state → 400 (route live; no Notion call) |

## 3. Phase 2 preparation

- **Selected video: `D26sUZ6DHNQ`, "99% of Developers Don't Get Sockets", 724 s (12:04).**
  - Educational (networking concept), so visual moments (diagrams/code) are likely, which makes frames
    and captions meaningful to verify.
  - Comfortably below 20 minutes.
  - **Transcript availability is already established by real evidence:** two real TRANSCRIBING
    checkpoints, each with 334 English segments. No extra YouTube call was needed.
  - It exercises exactly the path that failed before.
  - Alternative: `_S-cBzAg0GE` (8:38, an opinion talk with fewer likely visuals).
- The probe script (scratchpad `diag/gemini_t.py`, not in the repo) now uses the **deployed** timeout
  (300 s) instead of the old 120 s override. Its safety check refuses to send if RPD ≥ limit − 1 or a
  cooldown is active. Syntax was checked in the `13a9334` image; **not run**.
- **Note:** Phase 4 requires a click on Take Notes in real Chrome. Claude has no browser control in this
  environment, so the user must submit the job; Claude can then monitor and verify through the stack,
  the database and Notion's responses as recorded by the worker.

## 4. External API calls made

**None.** No Gemini, YouTube or Notion request.

## 5. Files changed

| File | Change |
|---|---|
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | Deployment section: deployed = commit `13a9334` (hash-verified), tag; limitation #2 = the blocked attempt; next steps (video, user submits via Chrome); session history |
| `docs/session_logs/e2e_attempt_blocked_by_quota.md` | this log |

No source, test, `.env` or Compose change. Services were not restarted.

## 6. Unresolved / next steps

1. **After 13:30 IST:** run the single probe (T1, 6-segment input, deployed 300 s timeout):
   `docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v <scratchpad>/diag:/diag:ro --entrypoint python worker /diag/gemini_t.py T1`
   - 200 → the user reloads the extension and clicks **Take Notes** once on `D26sUZ6DHNQ`; Claude
     monitors and verifies every stage;
   - 503 / 504 → Google is still unavailable; stop. A model change is the user's decision;
   - 429 → Google quota (not ours); stop.
2. Still open: README (R8); live demos (R6/R9).
