# Context recovery after the Copilot sessions + mandatory README (spec §8)

Date: 2026-11-21 IST.

Scope (one focused stage):
- recover the project state;
- record the user's three manual tests;
- write the mandatory root `README.md`.

Not done (by instruction):
- no application-code change;
- no real YouTube, Gemini or Notion call;
- no job;
- no `.env` change;
- no Git operation.
- Root `handover.md` untouched.

## 1. Investigated

- `docs/PROJECT_STATUS_AND_HANDOVER.md` (authoritative), `PROJECT_SPEC.pdf` (§2, §5, §6, §8), `docs/decisions.md`
  (ADR-01 … ADR-21), `docs/api.md`, `extension/README.md`, `.env.example`, `docker-compose.yml`,
  recent session logs (Flash-Lite switch, first Flash-Lite end-to-end job, network/SDK investigations).
- **Git:**
  - branch `phase-2`, HEAD `13a9334` ("ADR-21");
  - at the start, uncommitted from the Copilot sessions (later committed by the user as `14811c4`): `.env.example`, `backend/notesagent/common/config.py`,
    `docker-compose.yml`, two test files and the status document (Gemini model switch to
    `gemini-3.5-flash-lite`), plus untracked session logs and `test_unit_gemini_sdk_contract.py`;
  - **no root `README.md`**.
- **Running stack (read-only):**
  - all healthy; `/healthz` 200 with 2 live workers;
  - workers on image `6aad6362b38a` with the Compose-pinned `GEMINI_MODEL=gemini-3.5-flash-lite`;
  - gateway and publisher on `2a01b00b9603`.
- **Verified in code:**
  - the model default is `gemini-3.5-flash-lite` (`config.py`, `.env.example`, worker Compose literal);
  - `CHAOS_ENABLED` exists only in `.env.example`, with no code usage;
  - the Take Notes button never sends `notion_parent_id` (extension jobs always use
    `NOTION_DEFAULT_PARENT_ID`);
  - Notion page title = the video title; the first block = job marker + source URL;
  - error codes `NOTION_PARENT_MISSING` / `NOTION_OBJECT_NOT_FOUND`.

## 2. User-reported manual tests (recorded, not independently verified)

| Scenario | User report | Consistent job record (read-only SQL) |
|---|---|---|
| Normal video → Notion | success | `cb5eb8e1` `fyTxwIa-1U0` 419 s COMPLETED (result present); `a9ef5656` `D26sUZ6DHNQ` 724 s COMPLETED (result present) |
| Video > 20 min | handled as expected | `7c34510e` `jS9rBienEFQ` FAILED `VIDEO_TOO_LONG` |
| No captions | handled as expected | `d016a28e` `KXpHt1FvxZE` FAILED `YOUTUBE_TRANSCRIPT_UNAVAILABLE` |

Earlier `NOTION_OBJECT_NOT_FOUND` jobs (`574eb00f`, `f143c18a`) preceded the successful publishes.
Notion permissions and credentials were not touched.

## 3. Gap analysis against the specification

| Spec item | State |
|---|---|
| §1.1 components, §4 lifecycle, §5 API, health check | done |
| §2 extension (button, OAuth, popup, View in Notion) | done; real use user-reported |
| §3 pipeline + §7 constraints A/B | done; real runs user-reported plus earlier verified frame extraction |
| §6 failure modes | mechanisms implemented and tested with fakes; **live demonstration not prepared** |
| §8 README | **was missing → written now** |
| §9 evaluation (burst without LLM 429, latency near 20 min) | not measured |

**Next stage chosen: README.** It is a mandatory deliverable that was entirely missing, and it is the
reference the graders (and the later demos) need.

## 4. Completed

- **`README.md` (new, root).** Contents:
  - purpose; base URL; health check; the default Notion destination;
  - components; job states; prerequisites;
  - `.env` setup with a Fernet-key generation command that needs no host Python;
  - an environment-variable table covering everything the spec names (LLM key + model, configured rate
    limit, Notion OAuth client credentials, broker connection string, retry/backoff parameters) plus the
    other required variables;
  - Notion integration setup (redirect URI fixed by the manifest key) and the default-destination
    behaviour, including the failure codes;
  - starting the stack; loading the unpacked extension; the REST API summary;
  - scaling / killing workers; retry/DLQ behaviour with the demo profile; tests; stopping without `-v`.
- **Verification of the README against the repository:**
  - variable → service mapping checked against the rendered `docker compose config` (27 variables; two
    rows corrected: `INFRA_RETRY_DELAY_SEC`/`LOCK_RETRY_DELAY_SEC` also reach the gateway and publisher);
  - every listed variable exists in `.env.example`;
  - linked files exist; worker container names, `/healthz` fields, extension ID/key, gateway URL, the
    manifest host permission and the `npm test` script match;
  - the key-generation command produces a valid Fernet key (checked in the image, offline);
  - `docker compose config -q` is valid.
- **`docs/PROJECT_STATUS_AND_HANDOVER.md` updated:**
  - user-reported tests (new §5; the old §5 kept as history §5a);
  - PUBLISHING / extension / constraint-A / README rows;
  - limitations;
  - remaining work re-prioritized (next: the live failure demonstrations);
  - session history.

## 5. Files changed

| File | Why |
|---|---|
| `README.md` (new) | mandatory deliverable (spec §8) |
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | authoritative status: user tests, README, priorities |
| `docs/session_logs/readme_and_status_recovery.md` (new) | this log |

## 6. Commands and test results

| Command | Result |
|---|---|
| `git status`, `git log`, `git diff --stat` | as in §1 |
| read-only SQL on `jobs` (via `docker exec notes-agent-postgres-1 psql`) | as in §2 |
| `docker compose config --format json` (names only) + a Python mapping check | 25/27 rows matched initially; 2 corrected |
| `docker compose config -q` | valid |
| `cd extension && npm test` | **94 passed, 0 failed** |
| `docker compose --profile test build tests`; full backend suite (2026-11-21 23:54 → 2026-11-22 02:19) | **896 passed, 2 failed, 4 errors** (pytest time 18:47 while 2 h 25 min elapsed, so the host was almost certainly suspended during the run). All 6 were `ReadTimeout` / `ManagementUnavailable (TimeoutError)` against the RabbitMQ management API, in `test_work_queue_migration.py` (5) and `test_worker.py` (1) |
| re-run of the two affected files only | **28 passed, 0 failed** (275 s). So all 902 tests have passed against the current tree (896 in the full run + the 6 in the re-run); no code was changed in between |

## 7. Unresolved / limitations

- The README has not been followed end-to-end on a clean machine.
- The model-switch changes were committed by the user during this session (`14811c4`, 23:52), so the
  backend suite ran on that commit plus this session's doc-only changes. The gateway and publisher still run the older image `2a01b00b9603` (fine functionally: the
  model setting only affects the workers).
- §6 failure modes have not been demonstrated live. There is no fault-injection switch for 429s
  (`CHAOS_ENABLED` is unused).

## 8. Recommended next stage

**Live failure-handling demonstrations (spec §6) as a short, reproducible runbook:**
- worker kill mid-job;
- DLQ with the demo profile;
- graceful degradation;
- 429 handling: decide a safe method;
- scaling.

Each real job needs the user's approval.
