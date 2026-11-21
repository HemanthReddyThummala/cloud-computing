# One controlled live Gemini diagnostic

Date: 2026-11-21, 16:33 IST. Exactly one approved live Gemini API request was issued.

## Objective and configuration

Determine whether the configured `gemini-3.8-flash` model responds to a minimal plain-text request,
using the existing worker key and shared Redis limiter.

| Setting | Value |
|---|---|
| Worker | `notes-agent-worker-1` (healthy before and after the request) |
| SDK | Official `google-genai` 3.6.1 |
| Model / API version | `gemini-3.8-flash` / `v1` |
| Project | Existing `Default Gemini Project` configuration; no project identifier recorded |
| Authentication | Existing configured worker API key; read in memory only, never printed |
| Prompt | `Reply with exactly: Hello` |
| Request shape | Plain-text `generate_content`, no response schema, system instruction, or other generation options |
| SDK retries | Exactly 1 attempt, as set by the production `GeminiClient` |
| Timeout | Temporary in-memory settings copy set the production client timeout to 45 seconds; an outer 45-second guard was also used |
| Rate limiter | Existing shared Redis `RateLimiter.acquire(Provider.GEMINI, tokens=9)` using the production token estimator and default limiter wait policy |

The call reused `GeminiClient` and its configured API key. Its SDK client was used for one direct
plain-text `generate_content` call. No request was attempted before the limiter granted a permit.
There was no retry, model change, key rotation, or limiter bypass.

## Files and records inspected

Implementation and configuration:

- `backend/notesagent/worker/gemini.py`
- `backend/notesagent/worker/summarize.py`
- `backend/notesagent/worker/runner.py`
- `backend/notesagent/worker/stages.py`
- `backend/notesagent/common/ratelimit.py`
- `backend/notesagent/common/config.py`
- `backend/notesagent/common/http_errors.py`

Previous diagnostic records:

- `docs/session_logs/gemini_offline_http_request_investigation.md`
- `docs/session_logs/gemini_minimal_plaintext_probe.md`
- `docs/session_logs/gemini_new_key_limiter_scope_check.md`
- `docs/session_logs/gemini_503_root_cause_investigation.md`
- `docs/session_logs/e2e_attempt_blocked_by_quota.md`

The preflight confirmed SDK version `3.6.1`, configured model match, configured key presence, SDK
default API version `v1`, and no conflicting API-version override. Only booleans and non-secret
version/model facts were printed.

## Commands used

Read-only worker health and worktree check:

```powershell
docker ps --filter "name=notes-agent-worker-" --format "{{.Names}} {{.Status}}"
git status --short
```

Safe runtime preflight and the single request used an in-memory Python probe passed to the existing
worker. The command form was:

```powershell
$hex = [System.BitConverter]::ToString([System.Text.Encoding]::UTF8.GetBytes($probe)).Replace('-', '').ToLowerInvariant()
$code = 'exec(bytes.fromhex(__import__(chr(115)+chr(121)+chr(115)).argv[1]))'
docker exec notes-agent-worker-1 python -c $code $hex
```

The probe loaded settings from the worker environment without printing environment values, checked
the SDK/model/key-presence/API-version preconditions, obtained a real shared Redis limiter permit,
then made one SDK call. It printed only the outcome fields below; it did not print request headers,
the key, the response body, or raw exception text. The 45-second timeout override existed only in
the probe process.

## Live result

```text
limiter_allowed=true
request_sent=true
http_response_received=true
http_status=503
request_duration_sec=4.248
timed_out=false
provider_error_code=UNAVAILABLE
provider_error_message=This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.
request_id=not_returned_or_unavailable
error_type=ServerError
```

No safely available request ID was found in the SDK response's allowlisted request-ID headers
(`x-request-id`, `x-goog-request-id`, or `x-goog-gemini-request-id`). The only provider message
recorded is the sanitized error message above; no other response content or headers were recorded.

## Findings and unresolved questions

- The limiter granted the request; one permit was consumed normally. The limiter was not bypassed
  or manually changed.
- One HTTP response arrived: **503 `UNAVAILABLE`** in 4.248 seconds. This was not a 45-second timeout.
- The provider message reports high demand for the model. This is the service's reported reason, not
  proof of the underlying cause or definitive attribution of fault.
- The single request used the existing configured credentials and model, with the temporary 45-second
  timeout and one SDK attempt. No additional Gemini request was sent.
- It remains unknown whether the reported high demand was transient, model-specific, or related to
  project/account conditions. No Google console or service-status checks were performed in this task.

No application source, production configuration, dependencies, Docker configuration, Redis
configuration, or running service was changed. The workers remained healthy. Root `handover.md` was
not touched. Nothing was staged, committed, or pushed.

## Recommended next action and validation

Retain this sanitized 503 as incident evidence. Review the existing project's model availability,
quota, and service status in the authorized Google console; do not make configuration or billing
changes based on this single response. Do not send another live request without separate approval.

`git diff --check` passed after the documentation update.
