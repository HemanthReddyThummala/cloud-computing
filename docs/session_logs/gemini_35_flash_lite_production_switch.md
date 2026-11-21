# Production model switch to Gemini 3.5 Flash-Lite

Date: 2026-11-21, 16:59 IST.

## Objective and decision evidence

Switch the production summarization model from `gemini-3.8-flash` to exactly
`gemini-3.5-flash-lite`, using the prior successful one-chunk production-stage test as evidence.

The evidence reviewed before the switch:

- `gemini-3.8-flash` had repeated failures, including HTTP 503 responses and a minimal request with
  no response within 300 seconds.
- `gemini-3.5-flash-lite` returned HTTP 200 for a minimal plain-text request in 1.419 seconds.
- The production summarization stage then completed one structured fixture request in 1.381 seconds:
  three transcript segments, one chunk, no merge; production Pydantic parsing, normalization, and
  fixture-grounded timestamps passed.
- That structured test did not cover multi-chunk merging or a complete video job.

Those prior live results are documented in
`docs/session_logs/gemini_35_flash_lite_diagnostic.md` and
`docs/session_logs/gemini_35_flash_lite_structured_summarization_test.md`. No live Gemini request was
made during this model-switch session.

## Files inspected

- `docs/PROJECT_STATUS_AND_HANDOVER.md`
- `docs/session_logs/gemini_35_flash_lite_diagnostic.md`
- `docs/session_logs/gemini_35_flash_lite_structured_summarization_test.md`
- `backend/notesagent/common/config.py`
- `docker-compose.yml`
- `.env.example`
- `backend/tests/test_unit_summarize.py`
- `backend/tests/test_unit_gemini_failures.py`
- `backend/tests/test_unit_gemini_sdk_contract.py`
- `backend/tests/conftest.py`
- `backend/Dockerfile` and `backend/.dockerignore`

The previous structured test also recorded inspection of `gemini.py`, `summarize.py`, `stages.py`,
`ratelimit.py`, and the summarization fixtures.

## Changes made

- `docker-compose.yml`: pinned the worker service's `GEMINI_MODEL` to `gemini-3.5-flash-lite`, so an
  existing `.env` value cannot override this approved production selection. The configured worker
  count remains two.
- `backend/notesagent/common/config.py`: changed the application default and its validation example
  to Flash-Lite. No rate-limit values, request behavior, retry behavior, schema, prompt, or chunking
  logic changed. The limiter comment now records that its existing safety margin was set for 3.8 and
  has not been revalidated for Flash-Lite.
- `.env.example`: aligned the example model value with production.
- Updated the model-specific expectations in `test_unit_summarize.py`,
  `test_unit_gemini_failures.py`, and `test_unit_gemini_sdk_contract.py`.
- Updated the authoritative `docs/PROJECT_STATUS_AND_HANDOVER.md` with the switch, test and deployment
  results, and remaining risks.

The actual `.env` was not edited. Dependency manifests, runtime Docker settings other than the worker
model setting, API version, SDK version, and shared Redis limiter values were not changed.

## Commands and validation

Compose configuration was rendered and filtered to model/replica fields only:

```text
docker compose config --format json
rendered_worker_model=gemini-3.5-flash-lite
configured_replicas=2
```

Targeted offline tests:

```powershell
docker compose --profile test build tests
$env:MSYS_NO_PATHCONV = '1'
docker compose --profile test run --rm --no-deps tests pytest -q `
  tests/test_unit_summarize.py tests/test_unit_gemini_failures.py `
  tests/test_unit_gemini_sdk_contract.py
```

Result: **71 passed, 0 failed in 1.96 seconds.** These tests use fake or mock Gemini transports; they
made no Gemini API requests. The selected `test_unit_*` modules do not flush Redis state. The test
configuration uses the isolated `notes_test` database, not the production database.

Built the backend runtime image and recreated only the workers:

```powershell
docker compose build worker
docker compose up -d --no-deps --force-recreate --scale worker=2 worker
```

The resulting worker image was `notes-agent-backend:dev`,
`sha256:6aad6362b38a58dff512154d7a1807fe5551bf73521d473384ef29e89d69776f`. A no-network image
readback of `Settings().gemini_model` returned `gemini-3.5-flash-lite`.

Post-deployment checks:

```powershell
foreach ($name in @('notes-agent-worker-1', 'notes-agent-worker-2')) {
  docker inspect $name --format '{{.Name}} {{.State.Status}} health={{.State.Health.Status}} image={{.Image}}'
}
docker exec notes-agent-worker-1 python -c "from notesagent.common.config import get_settings; print(get_settings().gemini_model)"
docker exec notes-agent-worker-2 python -c "from notesagent.common.config import get_settings; print(get_settings().gemini_model)"
```

Both `notes-agent-worker-1` and `notes-agent-worker-2` were running and healthy, used the same new
image, and reported `gemini-3.5-flash-lite` from their loaded settings. The gateway, outbox publisher,
PostgreSQL, RabbitMQ, and Redis were not targeted by the `--no-deps` worker-only recreation and were
all running and healthy afterward.

`git diff --check` was run after the documentation update; it passed. No files were staged or
committed, nothing was pushed, no Redis counters were changed, and root `handover.md` was untouched.

## Unchanged settings and unresolved risks

- The shared limiter remains RPM 4 / TPM 200000 / RPD 16. These values were not adjusted; their
  safety margin was originally based on `gemini-3.8-flash`, so model-specific quota compatibility
  remains **Unverified**.
- The prior one-chunk structured test is evidence for only that small fixture. Multi-chunk chunking,
  merge requests, long transcripts, and a complete real video job remain **Unverified**.
- This configuration deployment itself did not call Gemini. No production model migration behavior
  beyond both workers loading the new setting was exercised.

Recommended next validation: a separately controlled multi-chunk Flash-Lite request plan/test,
followed by one short end-to-end video job if the shared limiter allows it.
