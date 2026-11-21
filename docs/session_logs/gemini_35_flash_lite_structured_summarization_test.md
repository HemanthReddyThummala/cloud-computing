# Gemini 3.5 Flash-Lite structured summarization test

Date: 2026-11-21, 16:48 IST. Exactly one approved live Gemini request was made through the existing
production summarization stage.

## Objective and test configuration

Test whether `gemini-3.5-flash-lite` can process a small transcript using the production summarization
prompt, JSON schema, generation settings, chunking, Pydantic parsing, and timestamp normalization.

| Setting | Value |
|---|---|
| Worker | `notes-agent-worker-1` |
| Model / API version | `gemini-3.5-flash-lite` / `v1` |
| SDK | Official `google-genai` 3.6.1 |
| Authentication | Existing configured API key and Google project; values not printed |
| Timeout / SDK attempts | 45 seconds per request; one SDK attempt |
| Limiter | Existing shared Redis Gemini limiter, called by the production stage before the SDK request |
| Request configuration | Production `CHUNK_SCHEMA`, `application/json`, `max_output_tokens=8192`, `thinking_level=LOW`, and production system instruction |
| Production configuration | Unchanged; the configured model remains `gemini-3.8-flash` |

The test used the existing small fixture
`backend/tests/test_unit_gemini_sdk_contract.py::SEGMENTS` (three timestamped segments). The fixture
includes an introduction to sockets, a diagram reference, and TCP server code. Its transcript text
is intentionally not reproduced here.

## Request-count preflight

Before any live request, the fixture was passed through the installed production
`chunk_budget()` and `chunk_segments()` functions using the worker's loaded settings:

```text
source_fixture=tests/test_unit_gemini_sdk_contract.py::SEGMENTS
segments=3
chunk_budget_tokens=12000
chunk_count=1
merge_requests=0
planned_gemini_requests=1
estimated_transcript_tokens=58
```

The request plan was reported before sending. Since the plan was one chunk and one request, the
authorized single request proceeded. No merge call was required.

## Files and records inspected

Production implementation and configuration:

- `backend/notesagent/worker/gemini.py`
- `backend/notesagent/worker/summarize.py`
- `backend/notesagent/worker/stages.py`
- `backend/notesagent/common/ratelimit.py`
- `backend/notesagent/common/config.py`

Summarization tests and fixture:

- `backend/tests/test_unit_summarize.py`
- `backend/tests/test_summarize_stage.py`
- `backend/tests/test_unit_gemini_sdk_contract.py`
- `backend/tests/support/fake_gemini.py`

Previous diagnostic records:

- `docs/session_logs/gemini_35_flash_lite_diagnostic.md`
- `docs/session_logs/gemini_offline_http_request_investigation.md`
- `docs/session_logs/gemini_minimal_plaintext_probe.md`
- `docs/session_logs/gemini_controlled_live_diagnostic_2026-11-21.md`
- `docs/PROJECT_STATUS_AND_HANDOVER.md`

The production path sends one request per transcript chunk. If there are multiple chunks, it also
sends one merge request. The chunk request uses `CHUNK_SCHEMA`; the merge request uses
`MERGE_SCHEMA`. One chunk therefore requires exactly one request and no merge. The stage takes a
Gemini limiter permit before every call, parses the response with `ChunkNotes.model_validate_json`,
then normalizes sections and visual moments to original transcript segment timestamps.

## Commands executed

Worker health and worktree inspection:

```powershell
docker ps --filter "name=notes-agent-worker-" --format "{{.Names}} {{.Status}}"
git status --short
```

The offline request-count calculation and the one live stage execution used in-memory Python probes
passed to the existing worker in this form:

```powershell
$hex = [System.BitConverter]::ToString([System.Text.Encoding]::UTF8.GetBytes($probe)).Replace('-', '').ToLowerInvariant()
$code = 'exec(bytes.fromhex(__import__(chr(115)+chr(121)+chr(115)).argv[1]))'
docker exec notes-agent-worker-1 python -c $code $hex
```

The live probe used `make_summarize_stage()` with a temporary in-memory settings copy changing only
the model and timeout. It used the real `GeminiClient`, fixture transcript, and shared Redis limiter.
The limiter wrapper only counted/delegated permits and guarded against exceeding the planned single
request. `GeminiClient` configured SDK retry attempts to one. No `.env` values, API keys, headers,
raw response body, or full transcript were printed. No pytest suite was run; the listed tests and
fixtures were inspected.

## Live result

```text
planned_gemini_requests=1
limiter_allowed=true
limiter_acquire_calls=1
request_count_actual=1
http_response_received=true
http_status=200
request_duration_sec=1.381
timed_out=false
production_pydantic_validation=passed
summary_nonempty=true
key_points_count=2
sections_count=2
section_timestamps_grounded=true
visual_moments_count=2
visual_timestamps_and_caption_reason_valid=true
dropped_sections=0
dropped_visual_moments=0
chunk_count=1
stage_gemini_requests=1
```

The SDK returned normally with structured output. Its installed HTTPX2 response path treats only
status 200 as a successful response and raises for other statuses. No provider error occurred. A
request ID was not captured by this stage probe.

## Output validation

- **JSON/schema:** The production `GeminiClient` request used the unchanged `CHUNK_SCHEMA` and JSON
  response configuration. `ChunkNotes.model_validate_json` passed, and the full production stage
  returned a `StageOutput`.
- **Summary and key points:** The summary was non-empty and two key points were produced. Manual
  review found they described sockets pairing an IP and port, and TCP server code, all present in
  the fixture.
- **Sections:** Two non-empty sections were produced at `0.0` and `30.0` seconds. Both timestamps
  map exactly to supplied segment starts; titles and points were non-empty.
- **Visual moments:** Two moments were produced at `4.0` and `30.0` seconds. Both timestamps map
  exactly to supplied segment starts. Caption and reason fields were non-empty and corresponded to
  the fixture's diagram and code references.
- **Grounding/content:** Manual review found no claims outside the short fixture. Production
  normalization dropped zero sections and zero visual moments.

The fixture text and generated notes are not copied into this log. This was a small, synthetic test
fixture, not a real video transcript.

## Conclusion, limitations, and next step

**Classification A for this one-chunk fixture:** the API request succeeded and the production
schema/normalization checks passed. The result is evidence that Flash-Lite handled this short
structured summarization request; it is not evidence that long or multi-chunk transcripts, merge
requests, or a full job will work. Production remains configured for `gemini-3.8-flash`.

The shared limiter granted one permit, which was consumed normally. No retry, additional request,
model test, key change, production configuration change, or Redis reset occurred. No application
source, dependency, Docker configuration, or running service was changed. Nothing was staged,
committed, or pushed; root `handover.md` was not touched.

If separately approved, the next useful validation is a controlled multi-chunk transcript test,
which requires one request per chunk plus one merge request. Do not migrate production automatically.

`git diff --check` passed after the documentation update.
