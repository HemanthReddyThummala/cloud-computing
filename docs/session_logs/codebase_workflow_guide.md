# Codebase and workflow guide (documentation only)

Date: 2026-11-23 IST. The user asked for a beginner-friendly guide explaining every important file,
every project-defined function, how they connect, the commands, and the full Take Notes → Notion
workflow.

Not done:
- no application code, configuration, Docker image, database, Redis or running-service change;
- no job; no external API call;
- `docs/PROJECT_STATUS_AND_HANDOVER.md` not modified (no factual inconsistency found that required
  it);
- root `handover.md` untouched;
- no Git operation;
- no `.env` read.

## 1. Files created

| File | Description |
|---|---|
| `docs/CODEBASE_AND_WORKFLOW_GUIDE.md` | The guide: 16 sections, about 2,300 lines and 22,000 words, 5 Mermaid diagrams |
| This log | — |

The guide was written as 8 parts in the session scratchpad and concatenated. No other repository file
changed.

**Guide structure:**
1. How to read
2. Concepts (16 topics, each defined before use)
3. Architecture diagrams: the four components, submission and delivery, the worker pipeline, the
   state lifecycle, rate limiting
4. Folder and file structure: a table per area, with path, purpose, responsibility, contents,
   connections and workflow position
5. The 13-step workflow table plus step details
6. Failure handling: classification, crashes and redelivery, idempotency, DLQ, rate limiting, frame
   degradation
7. Function reference: `common/`
8. Function reference: gateway
9. Function reference: outbox
10. Function reference: worker and stages
11. Function reference: `ops/` and scripts
12. Function reference: extension
13. Tests
14. Command reference: 11 categories, each command with where, what/why, expected result and safety
15. Debugging table
16. Unclear points and limits

## 2. Sources inspected

- `docs/PROJECT_STATUS_AND_HANDOVER.md` (status, live versus test-only evidence).
- **All application source:**
  - 46 Python modules under `backend/notesagent/`;
  - 9 JavaScript files under `extension/` plus `manifest.json` and `package.json`;
  - `backend/migrations/001–004`;
  - `docker-compose.yml`, `backend/Dockerfile`, `backend/infra/rabbitmq/rabbitmq.conf`,
    `backend/pytest.ini`;
  - `scripts/migrate_work_queue.py`.
- **Read in full or in large part:** `runner.py`, `submission.py`, `publisher.py`, `stages.py`,
  `transcribe.py`, `summarize.py`, `frames.py`, `publish.py`, `notion.py`, `gemini.py`,
  `retry_policy.py`, `jobs_repo.py` (retry/dead-letter/transition/idempotency), `states.py`,
  `health.py`, `main.py`, `routes.py`, `validation.py`, `gateway/youtube.py`, `oauth.py`, `crypto.py`,
  `broker.py`, `dlq_audit.py`, `dead_letters.py` (parse/decide), `logging.py`, `migrate.py`,
  `runtime.py`, the entry points, `db.py`, `errors.py`, `ratelimit.py` (acquire/check/settle/report),
  `http_errors.py` (`classify_http`, `_gemini`, `_youtube`, `classify_exception`),
  `worker/youtube.py` (client, transcript classifier, patterns), `ops/rabbitmq_mgmt.py` (`_request`),
  and every extension file.
- **Test layout:** the file names of `backend/tests/` and `extension/tests/`, `conftest.py`
  (isolation), and the Compose `tests` service.
- **Commands:** taken from `README.md`, `docs/FAILURE_DEMONSTRATION_RUNBOOK.md` and
  `docker-compose.yml`. No command was invented.

## 3. Method and major decisions

**Inventory:**
- A Python `ast` script listed every function, method and class with its signature, decorators,
  docstring, the project functions it calls, and a name-based "called by" index.
- The name-based index is noisy for common names (`__init__`, `open`, `close`, `format`). Callers in
  the guide were therefore corrected from the code, not copied from the index.

**Usage check** (`grep` of the app and test trees) for functions with no application caller:
- **Unused:** `BrokerClient.publish_job_event`, `notion.is_permanent`.
- **Tests only:** `raise_classified`, `legacy_work_queue_arguments`, `all_transition_pairs`.
- **Reached through a framework, callback or thread:** FastAPI routes, lifespan and exception
  handlers; Pydantic validators; aio-pika consumer callbacks (`_on_message`, `on_message`);
  `asyncio.to_thread` (`extract_frame`, `download_video`, `probe_duration`, `purge_stale`);
  `transcribe.call()` arguments (`video_info`, `transcript_list`, `fetch`, `classify_transcript_error`,
  `before_youtube_metadata`).

**Format decisions:**
- Important functions get a full block: meaning, why, inputs, output, inside, calls, called by, role.
- Trivial helpers are grouped in tables with the same fields in short form.
- Test functions are explained per file, not individually (they are not application code).
- Library calls are labelled *library:* / *Chrome:*, so they are never confused with project
  functions.

**Corrections made during writing** (checked against the code before finishing):

| Draft said | Code says |
|---|---|
| Migration 003 adds "retry state" | It adds `infra_attempts`, outbox destination columns and `dead_letter_audit`; `retry` and `attempts` are from 001 |
| — | `rabbitmq.conf` values added from the file (`consumer_timeout = 3600000`, memory and disk limits) |
| — | Migration 002 constraint names added |
| `extract_video_id` accepts "a Shorts-style path" | The exact hosts and the prefixes `shorts`, `embed`, `live`, `v` |
| `classify_transcript_error` mapping (incomplete) | Full order, including age-restricted, unplayable, and `YOUTUBE_TRANSCRIPT_TEMPORARY` |
| A diagram label with HTML entities | Plain braces inside a quoted label |

## 4. Verification performed

| Check | Result |
|---|---|
| Coverage of backend definitions (functions, methods, classes, via `ast`; whole-word search in the guide) | **401 / 401** present (342 functions/methods + 59 classes) |
| Coverage of extension definitions (functions, `const … = (…)` / `async (…)`, classes) | **67 / 67** present |
| Table-of-contents links vs headings (GitHub-style anchors) | 16 links, **0 broken** |
| Section numbering | 1–16 sequential |
| Mermaid | 5 blocks, reviewed by hand against the code (component edges, submission sequence, runner pipeline, the 16 transitions of `ALLOWED_TRANSITIONS`, limiter flow); no unsafe angle-bracket tokens. Not machine-rendered (`mermaid-cli` not installed) |
| Secrets | Scan for Google keys, Notion tokens, sealed tokens, Fernet ciphertext, extension ID, redirect URL, e-mail: **none** |
| Claims vs code | Spot-verified against the source: topology exchange types, idempotency SQL, validators, the `QueueStats` fields, the management `_get`/404 behaviour, the transcript classifier, `_PROGRESS` values, heartbeat file ages in the Compose health checks |

## 5. Commands executed (all read-only)

- `git ls-files`, `wc -l` (inventory); a Python `ast` index script (output in the scratchpad).
- `sed`, `grep`, `cat` on source, migrations, Compose, Dockerfile, README and runbook.
- `grep -rnw` usage counts in the app and test trees.
- A coverage and anchor validation script; a secret scan.
- `cat` of the 8 scratchpad parts into `docs/CODEBASE_AND_WORKFLOW_GUIDE.md`.

No container command, job submission or external request was made.

## 6. Unresolved questions

- The Redis Lua scripts, all branches of `http_errors._notion`, and a few validators are described
  from their structure, docstrings and ADR-10/13, not walked line by line (stated in guide §16).
- The commands are taken from the project's own docs and were used during the project, but they were
  not re-executed in this session; PowerShell and Bash variants are both listed.
- No factual inconsistency with `docs/PROJECT_STATUS_AND_HANDOVER.md` was found. The status document
  was therefore not modified.
