# Single `gemini-4-hydrogen` probe → 503 UNAVAILABLE

Date: 2026-11-21 IST (2026-11-20 23:30 UTC). Approved by the user: exactly one real Gemini request.

## 1. Conditions

| Item | Value |
|---|---|
| Method | throwaway container: `docker compose run --rm --no-deps -T -e PYTHONPATH=/app -e GEMINI_MODEL=gemini-4-hydrogen -v <scratchpad>/diag:/diag:ro --entrypoint python worker /diag/gemini_t.py T1` |
| Model | `gemini-4-hydrogen`, overridden **only in that container**. Production `.env` and workers unchanged (`GEMINI_MODEL=gemini-3.8-flash`, verified afterwards) |
| Code path | production `make_summarize_stage()` → `ask()` → shared `RateLimiter.acquire()` → `GeminiClient.generate()` (`v1 …:generateContent`, `response_json_schema`, system instruction, thinking `LOW`, `max_output_tokens` 8192) |
| Limiter | thresholds unchanged (Compose: RPM 4, TPM 200000, RPD 16); RPD before = 1 |
| Timeout | 300 s (deployed setting) |
| Retries | SDK retries off (`HttpRetryOptions(attempts=1)`); the script blocks any second call; no rerun |
| Input | stored 6-segment TRANSCRIBING checkpoint of job `9302ffd9` (database read-only) |

## 2. Result (sanitized, complete)

```
precheck: pacific_day=2026-11-20 rpd_used=1 rpd_limit=16 cooldown_keys=0 model=gemini-4-hydrogen thinking=low
          max_output_tokens=8192 timeout_sec=300.0 segments=6 video_id=jNQXAC9IVRw
outcome:  FAILED  code=GEMINI_HTTP_503
          classified_detail={"http_status": 503, "provider_code": "UNAVAILABLE", "retry_after_sec": null}
gemini:   calls=1  sent_at=23:30:07.329Z  answered_at=23:30:11.198Z  duration_sec=3.87
          exception_type=ServerError  http_status=503  google_status=UNAVAILABLE
after:    rpd_used=2
exit 1   (23:30:05 → 23:30:11 UTC)
```

| Question | Answer |
|---|---|
| Reached Google? | **Yes**: one SDK call, Google returned HTTP 503 |
| HTTP status / provider error | **503 / `UNAVAILABLE`**, no Retry-After |
| Valid structured JSON notes? | **No**: no response body to parse |
| Our parsing/validation accepted it? | not reached (no output) |
| Tokens / latency | no usage returned; 3.87 s to the 503 |
| Exactly one request? | **Yes** (`calls=1`; RPD 1 → 2) |
| Secrets | 0 key-like strings; no `.env` content shown |

Notably **not** 404 or 400: the model name and our `v1 generateContent` request were accepted far enough
for Google to answer `UNAVAILABLE`, not `NOT_FOUND` or `INVALID_ARGUMENT`.

## 3. Interpretation (evidence vs hypothesis)

**Evidence — every request since 2026-11-20 ~19:35 IST got 503/504 (17 requests):**

| Project | Model | Result |
|---|---|---|
| old | `gemini-3.8-flash` | 503/504 (15 requests) |
| new | `gemini-3.8-flash` | 503 (1) |
| new | `gemini-4-hydrogen` | 503 (1) |

The 6-segment input that succeeded once on 2026-11-20 (3.8-flash, old project) now fails on both models.

**Hypotheses (not provable from our side):**
- **H-a:** a broad Gemini API capacity or availability problem for the free tier (both Flash-class models
  affected).
- **H-b:** something specific to **our request shape** that triggers server-side `UNAVAILABLE` (the `v1`
  generateContent path combined with `responseJsonSchema` + `thinkingLevel` + system instruction +
  `X-Server-Timeout: 300`), whereas AI Studio uses another route (the Interactions API).
  - Against H-b: the identical shape succeeded once on 2026-11-20, and a malformed or unsupported
    parameter normally yields 400, not 503.
  - It cannot be excluded without a differently-shaped request.

**The smallest discriminating test (NOT performed; needs approval): 1 request** through the same SDK,
model and key with a **minimal plain-text prompt**: no schema, no system instruction, no thinking config,
default timeout.
- 200 → H-b: the problem lies in a request feature (investigate which).
- 503 → H-a: the service is unavailable for API-key requests.

## 4. Not done (per instructions)

- No retry.
- No model switch in production; no `.env`, code, worker, API-version or limiter change.
- No job.
- No YouTube or Notion calls.
- No Git operations.
- Root `handover.md` untouched.

## 5. Files changed

- `docs/PROJECT_STATUS_AND_HANDOVER.md`: §5 evidence (Hydrogen also 503), the history row.
- This log.
