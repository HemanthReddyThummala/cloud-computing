# Specification compliance review, final test run and deliverables

Date: 2026-11-22 IST. The user asked to check that all mandatory spec requirements are complete, run
all tests, and produce the deliverables required by `PROJECT_SPEC.pdf`.

Not done:
- no job, no external API call;
- no configuration, container, Redis or database change (production);
- no Git;
- root `handover.md` untouched.

## 1. Spec read

`PROJECT_SPEC.pdf`, 3 pages:
- §1 components;
- §2 extension;
- §3 pipeline;
- §4 lifecycle;
- §5 REST API;
- §6 failure handling (live);
- §7 Constraints A/B;
- §8 deployment and deliverables;
- §9 evaluation;
- §10 design choices.

## 2. Tests

| Suite | Result |
|---|---|
| `docker compose --profile test build tests`, then `pytest -p no:warnings -q -rfE --tb=short` (full backend, isolated test DB / vhost / Redis db) | **951 passed, 0 failed** in 830.65 s; ended 16:14:00 IST |
| `cd extension && npm test` | **94 passed, 0 failed** (9 suites) |

**Production after the run:**
- `/healthz` ok, 2 live workers, both healthy with 0 restarts;
- 24 jobs (unchanged);
- limiter `rpd:2026-11-22` = 12 (unchanged).

## 3. Code checks against the spec (read-only)

**§5 API:**
- `SubmitJobRequest` has `youtube_url`, `notion_token` and optional `notion_parent_id`;
- `POST /jobs` returns **202** with `Location: /jobs/{id}`;
- errors are `{"error": {"code", "message"}}`;
- an unknown job → **404** `JOB_NOT_FOUND`;
- `/results` before terminal → **409** `JOB_NOT_FINISHED`; otherwise it returns `status`, `result`,
  `warnings`;
- `/healthz` → 200/503.

**§4:** all 9 states exist in `common/states.py`.

**§7-A:** `check_duration` (`VIDEO_TOO_LONG`) runs before the transcript request.

**§7-B:**
- frames come only from FFmpeg (`-ss <t> -frames:v 1`), with no generation;
- every Notion image block carries `frame_caption` (timestamp link + LLM caption);
- failed frames become warnings and are omitted.

**§2:** SPA navigation via `yt-navigate-finish` / `popstate`, plus 3 content tests.

**§6 429 for all three APIs, tests:**
- `test_youtube_429_uses_shared_cooldown_and_the_existing_retry`;
- `test_gemini_429_retry_info_is_honoured_and_shared`;
- `test_notion_429_is_retried_and_publishes_once`;
- `test_download_429_is_retried_through_the_outbox`;
- `test_provider_429_in_one_worker_cools_down_the_others`.

**§8 README:** every required item is present: base URL; `NOTION_DEFAULT_PARENT_ID`;
`GEMINI_API_KEY` / `GEMINI_MODEL`; the rate limits; `NOTION_CLIENT_ID` / `SECRET` / `REDIRECT_URI`;
`RABBITMQ_URL`; `MAX_ATTEMPTS` / `BACKOFF_SCHEDULE_SEC`; `/healthz`; scaling; Load unpacked.

## 4. Result: no mandatory requirement is missing

Every §1–§8 requirement is implemented, with tests, and where possible verified live (see
`docs/SPEC_COMPLIANCE.md` for the full matrix).

**Documented gaps (not missing features):**
1. **§6 429, live:** only Gemini (through the ADR-22 demo switch, which raises the real exception
   type). YouTube and Notion 429 handling is implemented and covered by automated tests on real
   infrastructure. Real 429s cannot be produced on demand.
2. **§6 DLQ:** the spec's suggested trigger (a private video) ends `FAILED` by design (ADR-14 D-1,
   non-retryable). The DLQ is demonstrated with a retryable fault and with real 503 / daily-limit
   cases.
3. **§3 chunk-and-merge:** tests only. Ordinary videos ≤ 20 min fit one chunk.
4. **§9 burst:** 3 clicks without an LLM 429. Live pacing above RPM 4 is tests only.
5. **§8 README:** not yet followed from a clean checkout on another machine.
6. **External:** intermittent real Gemini 503s affect latency.

## 5. Deliverables

All §8 deliverables already existed (`extension/`, `docker-compose.yml`, `README.md`, `.env.example`,
`/healthz`, `docs/api.md`).

| File | Change |
|---|---|
| `docs/SPEC_COMPLIANCE.md` (new) | Professor-facing matrix: each spec requirement → implementation → evidence level (Live / Tests / Gap), the measured §9 results, and the known gaps. ADR references were checked against `docs/decisions.md` (two wrong numbers in the draft were corrected before saving the final version) |
| `README.md` | "Further documentation": links to `SPEC_COMPLIANCE.md`, the runbook and `docs/api.md`; the stale "ADR-01 … ADR-21" corrected to ADR-23 |
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | tests table (final 951 run), §10 history row |
| This log | — |

## Unresolved

- The gaps in §4 above (documented; none is a missing mandatory feature).
- Commit: done by the user (see `final_pre_submission_audit.md` for the commands). The path list there
  covers `docs/` (`SPEC_COMPLIANCE.md` included) and `README.md`.
