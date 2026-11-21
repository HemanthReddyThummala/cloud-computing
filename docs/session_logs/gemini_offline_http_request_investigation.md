# Gemini offline HTTP request-path investigation

Date: 2026-11-21, 16:02 IST. All HTTP interactions in this investigation used an in-process mock
transport. **No HTTP request was sent to Google; no quota was consumed.**

## 1. Files and path inspected

Application, test, configuration and dependency files:

- `backend/notesagent/worker/runner.py`
- `backend/notesagent/worker/stages.py`
- `backend/notesagent/worker/summarize.py`
- `backend/notesagent/worker/gemini.py`
- `backend/notesagent/common/config.py`
- `backend/notesagent/common/http_errors.py`
- `backend/notesagent/worker/retry_policy.py`
- `backend/notesagent/common/jobs_repo.py`
- `backend/tests/test_unit_gemini_sdk_contract.py`
- `backend/requirements.in`
- `backend/requirements.txt`
- `docker-compose.yml`

Existing records used for the success/failure comparison:

- `docs/session_logs/phaseB_part2_gemini_summarization.md`
- `docs/session_logs/gemini_503_root_cause_investigation.md`
- `docs/session_logs/gemini_controlled_tests_T1_T2.md`
- `docs/session_logs/gemini_integration_root_cause_diagnosis.md`
- `docs/session_logs/first_real_job_failure_diagnosis.md`
- `docs/PROJECT_STATUS_AND_HANDOVER.md`

The call path is:

1. `JobWorker._run_stage()` in `worker/runner.py` selects the stage using the job status and awaits it.
2. `build_stage_registry()` in `worker/stages.py` maps `SUMMARIZING` to
   `make_summarize_stage(settings, gemini)`.
3. `summarize()` in `worker/summarize.py` reads the `TRANSCRIBING` checkpoint, chunks transcript
   segments, constructs the system instruction and prompt, and calls its `ask()` helper.
4. `ask()` takes a limiter permit, lazily creates `GeminiClient`, then calls `generate()`.
5. `GeminiClient.generate()` builds `GenerateContentConfig` and awaits
   `client.aio.models.generate_content(...)` from `google-genai`.
6. The installed SDK serializes and sends the request through HTTPX2. In this offline verification,
   HTTPX2's transport was replaced with `httpx2.MockTransport`.

The test helper in `test_unit_gemini_sdk_contract.py` already uses this same SDK-supported mock
transport pattern; the test file was read but not modified.

## 2. Offline capture command and safety

The capture ran inside the existing healthy `notes-agent-worker-1` container. The Python probe was
held in memory as `$probe`, UTF-8-hex encoded as `$hex`, and invoked as:

```powershell
$code = 'exec(bytes.fromhex(__import__(chr(115)+chr(121)+chr(115)).argv[1]))'
docker exec notes-agent-worker-1 python -c $code $hex
```

The probe used a synthetic transcript, synthetic fake key, in-memory limiter stub, real production
`make_summarize_stage` and `GeminiClient`, the installed `google-genai` 3.6.1 SDK, and an
`httpx2.MockTransport` handler. Its client wrapper replaced only the transport in the SDK options:

```python
options = http_options.model_copy(update={
    "async_client_args": {"transport": httpx2.MockTransport(handler)}
})
return real_client(api_key=api_key, http_options=options)
```

The handler returned synthetic success/error responses or raised a synthetic `httpx2.ReadTimeout`.
It captured request metadata and returned only booleans for sensitive header presence. The API-key
header value, authorization values, request text, `.env`, and any secret were never printed. The
captured URL had no query string. The mock transport intercepted every HTTP request in the probe.

No application code, configuration, `.env`, container state, Redis state or running service was
changed. No package was installed. No Git staging, commit or push was performed; root `handover.md`
was not touched.

## 3. Captured production request

The production stage completed against the synthetic 200 response. The mock transport observed:

| Property | Captured result |
|---|---|
| Method | `POST` |
| Sanitized endpoint | `https://generativelanguage.googleapis.com` |
| Path | `/v1/models/gemini-3.8-flash:generateContent` |
| API version / model | `v1` / `gemini-3.8-flash` |
| Query string | Absent |
| Top-level body keys | `contents`, `systemInstruction`, `generationConfig` |
| User contents | One `user` message with one text part (synthetic text not printed) |
| System instruction | Present as a `user` instruction with one part |
| Generation config keys | `maxOutputTokens`, `responseJsonSchema`, `responseMimeType`, `thinkingConfig` |
| JSON response format | `application/json` |
| Schema | Object; required `summary`, `key_points`, `sections`, `visual_moments`; serialized size 727 characters |
| Max output tokens | `8192` |
| Thinking config | `{"thinking_level": "LOW"}` |
| Tools / temperature / topP / topK | Absent |
| Expected headers | `content-type`, `x-goog-api-key`, `x-goog-api-client`, `x-server-timeout` present; `authorization` absent |
| Content type / server timeout | `application/json` / `300` |
| SDK options | timeout `300000` ms; retry attempts `1`; no explicit API-version override |
| HTTPX2 request timeout extension | connect/read/write/pool each `300.0` seconds |
| Mock calls for success | 1; stage checkpoint recorded 1 Gemini request |

The generated URL is the expected Google AI `v1 generateContent` endpoint. The API-key header was present; its value was not captured in output. The existing contract test also
asserts that the fake key is absent from the URL and body. The API version was not set explicitly by
application code; the observed request path confirms the SDK default was `v1`.

## 4. Retry and error handling checks

The mock returned one synthetic 503 and one synthetic 504. Each error case produced exactly one
transport-handler invocation with SDK `retry_attempts=1`:

| Mock response | Classified result | Status details retained |
|---|---|---|
| HTTP 503, provider status `UNAVAILABLE` | `TransientError`, `GEMINI_HTTP_503` | `http_status=503`, `provider_code=UNAVAILABLE` |
| HTTP 504, provider status `DEADLINE_EXCEEDED` | `TransientError`, `GEMINI_HTTP_504` | `http_status=504`, `provider_code=DEADLINE_EXCEEDED` |

A synthetic `httpx2.ReadTimeout` was classified as `TransientError` / `GEMINI_UNREACHABLE`, with
only the safe exception type `ReadTimeout` retained. These results confirm SDK retries are disabled
for one attempt; the separate application retry policy can still schedule a later job attempt.

Source inspection also confirms that `classify_gemini_error()` passes SDK `APIError` status, body
and response headers into `classify_http()`. The status and whitelisted provider code are included
in the classified error, then `retry_policy.provider_status()` carries them into retry/dead-letter
records and sanitized event/log details. Error message text, raw headers and secrets are not kept.
The stage suppresses the original exception chain with `raise ... from None` after classification;
this is deliberate sanitization, not loss of the tested status/provider code.

Transport errors (including HTTPX2 timeout errors) are mapped to retryable `GEMINI_UNREACHABLE`.
`asyncio.CancelledError` is not swallowed by the stage's `except Exception`; the runner explicitly
re-raises cancellation. Production has 300-second per-phase HTTP timeouts and a server timeout
header, but there is no separate total elapsed-time timeout around `generate()`. A continuously
progressing request could therefore exceed 300 seconds overall. Cancellation/resource cleanup for a
real socket was not exercised; the mock tests do not establish that behavior.

## 5. Production versus prior successful request

The current mock capture verifies request construction and uses synthetic transcript content; it is
not itself a live API result. The historical comparison below comes only from the existing session
records:

| Property | Prior successful request | First 503 | Current production construction |
|---|---|---|---|
| Model / API version | `gemini-3.8-flash` / `v1` | Same | `gemini-3.8-flash` / `v1` |
| Endpoint | `v1 ...:generateContent` | Same | Captured exact `v1` endpoint above |
| Body config | JSON schema, JSON MIME, `LOW` thinking, 8192 output tokens | Recorded same | Captured same configuration and schema size |
| SDK / backend | `google-genai` 3.6.1 / HTTPX2 | Same | `google-genai` 3.6.1 / HTTPX2 |
| SDK attempts | 1 | 1 | 1; one call observed per mock error |
| `X-Server-Timeout` | 120 | 120 | 300 |
| Result | HTTP 200 in 32.7 s | HTTP 503 in 4.29 s | Not tested against Google |

The earlier record says the successful and first failed production requests had the same code,
endpoint, input, key/project, model, headers and body; only their time/result differed. The
confirmed request-level change relevant here is the timeout/deadline: earlier 120 seconds versus
current 300 seconds. Later fast 503 responses also arrived well before the 300-second deadline.
The historical 504s around 119 seconds align with the earlier 120-second deadline.

The snake-case `thinking_level` field is what the installed SDK serialized in the current request;
the existing success record reports the same field. No unexpected sampling/tool parameters, model
switch, API-version switch, custom endpoint or schema change was found that explains the 503s.
The timeout difference could be relevant to deadline-related 504s, but does not explain the fast
503s.

## 6. A-D findings

### A. Verified facts

- The mocked production `SUMMARIZING` stage emits the expected `POST` endpoint, `v1` path, model,
  structured JSON body and expected header presence.
- The configured 300-second timeout reaches HTTPX2 as 300 seconds for connect, read, write and pool,
  and as `X-Server-Timeout: 300`.
- SDK retry attempts are `1`; each synthetic 503/504 made one mocked HTTP call.
- 503/504 status and provider codes survive classification; synthetic timeouts become
  `GEMINI_UNREACHABLE`.
- The prior success and first 503 used the same recorded request construction and a 120-second
  timeout; current production uses 300 seconds.

### B. Possible causes supported by evidence

- The old 120-second server deadline is consistent with the historical 504s around 119 seconds.
- A response/transport problem after TLS remains possible; mock transport cannot reproduce the live
  HTTP response path.
- The lack of a total elapsed-time watchdog could permit a long request if its network phases
  continue to make progress and the server deadline is not enforced as expected.

### C. Causes ruled out

- An incorrect production endpoint, model, API version, request schema, unexpected generation
  parameter, missing expected header, absent 300-second transport timeout, or enabled SDK retries.
- Loss of HTTP status/provider code for the tested 503/504 errors.

### D. Unresolved questions

- Why the real minimal request received no response and why identical recorded production requests
  returned 200 and 503 at different times.
- Whether a live endpoint enforces the `X-Server-Timeout` consistently.
- Whether real socket cancellation and connection cleanup behave as expected on worker cancellation.

## 7. Recommended next action and validation

No request-construction defect was found, so no correction is proposed or applied. Review existing
sanitized provider response evidence and keep future validation offline/mock-based where possible.
Any further live Gemini request requires separate approval and could consume quota.

The offline capture completed successfully with one fake success request, one request each for fake
503 and fake 504, and one fake timeout. No `pytest` suite was run; the existing contract-test file
was inspected but not modified. `git diff --check` was run after documentation edits and passed.
