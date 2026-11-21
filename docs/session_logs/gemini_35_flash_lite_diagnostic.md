# Controlled Gemini 3.5 Flash-Lite diagnostic

Date: 2026-11-21, 16:43 IST. Exactly one approved live Gemini API request was issued.

## Objective and test configuration

Check whether `gemini-3.5-flash-lite` responds to the requested minimal plain-text prompt, using the
existing API key, project setup, official SDK, and shared Redis Gemini limiter.

| Setting | Value |
|---|---|
| Worker | `notes-agent-worker-1` |
| SDK | Official `google-genai` 3.6.1 |
| Model / API version | `gemini-3.5-flash-lite` / `v1` |
| Project / authentication | Existing `Default Gemini Project` setup and configured worker key; no identifier or key value recorded |
| Prompt | `Reply with exactly: Hello` |
| Request shape | Plain-text `generate_content`, without a schema, system instruction, or other generation options |
| SDK attempts | 1, using the production `GeminiClient` SDK options |
| Timeout | 45 seconds in the temporary in-memory settings copy, plus an outer 45-second guard |
| Rate limiter | Existing shared Redis limiter; permit requested for 9 estimated input tokens using the production token estimator |

The probe copied the loaded settings in memory to override only the model and timeout, then used
`GeminiClient` and its configured API key for one direct plain-text SDK call. It did not change
production configuration or code. No request was attempted before the limiter granted a permit.

## Files and previous records inspected

Implementation/configuration:

- `backend/notesagent/worker/gemini.py`
- `backend/notesagent/worker/summarize.py`
- `backend/notesagent/worker/runner.py`
- `backend/notesagent/worker/stages.py`
- `backend/notesagent/common/ratelimit.py`
- `backend/notesagent/common/config.py`

Previous diagnostic records:

- `docs/session_logs/gemini_controlled_live_diagnostic_2026-11-21.md`
- `docs/session_logs/gemini_offline_http_request_investigation.md`
- `docs/session_logs/gemini_minimal_plaintext_probe.md`
- `docs/session_logs/gemini_503_root_cause_investigation.md`
- `docs/session_logs/gemini_4_hydrogen_probe.md`
- `docs/PROJECT_STATUS_AND_HANDOVER.md`

The worker preflight confirmed SDK version `3.6.1`, that the configured key was present, and that
the SDK default API version was `v1` with no conflicting API-version override. Only non-secret
version/model facts and booleans were printed.

## Commands used

Worker health and worktree inspection:

```powershell
docker ps --filter "name=notes-agent-worker-" --format "{{.Names}} {{.Status}}"
git status --short
```

The preflight and one-request probe were held in memory and passed to the existing worker in this
form:

```powershell
$hex = [System.BitConverter]::ToString([System.Text.Encoding]::UTF8.GetBytes($probe)).Replace('-', '').ToLowerInvariant()
$code = 'exec(bytes.fromhex(__import__(chr(115)+chr(121)+chr(115)).argv[1]))'
docker exec notes-agent-worker-1 python -c $code $hex
```

The probe loaded settings without printing environment values, checked SDK/model/key-presence/API
version preconditions, called the real shared limiter, then made one SDK request. Output was limited
to the safe result fields below; no key, headers, raw exception text, or response body was printed.

After the request, the installed SDK source was inspected offline. For its HTTPX2 response path,
`raise_for_async_response` returns normally only for status `200` and raises for other statuses.
The generated SDK response did not expose a raw status field, but the SDK returned normally with
text matching the requested response; therefore the successful HTTP status was 200.

## Result

```text
limiter_allowed=true
request_sent=true
http_response_received=true
http_status=200
request_duration_sec=1.419
timed_out=false
response_matches_expected=true
request_id=not_returned_or_unavailable
```

No provider error code or message was returned. No safe request ID was found in the checked
allowlisted headers (`x-request-id`, `x-goog-request-id`, or `x-goog-gemini-request-id`).

## Findings and unresolved questions

- The shared limiter allowed the request; one permit was consumed normally. The limiter was not
  bypassed or reset.
- The SDK returned a successful response matching `Hello` in 1.419 seconds. Its installed HTTPX2
  handling confirms this was HTTP 200, not a timeout.
- Exactly one request was sent with one SDK attempt. No additional model or key was tested.
- This establishes only that the minimal plain-text request worked for `gemini-3.5-flash-lite`.
  The production structured summarization request and its schema/settings remain untested with this
  model.
- No production model configuration was changed. The configured production model remains
  `gemini-3.8-flash`.

No application source, production settings, dependencies, Docker configuration, Redis configuration,
or running service was changed. No secrets were printed. Nothing was staged, committed, or pushed;
root `handover.md` was not touched.

## Recommended next action and validation

If separately approved, test the production structured summarization request with
`gemini-3.5-flash-lite` using the shared limiter. Review that result before considering any model
configuration change; do not migrate production automatically.

`git diff --check` passed after the documentation update.
