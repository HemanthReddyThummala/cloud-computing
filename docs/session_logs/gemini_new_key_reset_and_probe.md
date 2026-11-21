# New Gemini key: targeted limiter reset + one probe → 503 UNAVAILABLE

Date: 2026-11-21 IST (2026-11-20 23:20 UTC).

**User confirmation:** the new Gemini API key belongs to a **different Google Cloud project** from the old
key (previous session: `gemini_new_key_limiter_scope_check.md`).

## 1. Targeted reset (Redis db 0)

| Step | Command | Result |
|---|---|---|
| Before | `redis-cli -n 0 --scan`, `DBSIZE` | `ratelimit:gemini:rpd:2026-11-20`, 2 × `workers:heartbeat:<host>-1`; DBSIZE 3 |
| Delete | `redis-cli -n 0 DEL ratelimit:gemini:rpd:2026-11-20` | `1` (exactly one key deleted) |
| After | `EXISTS ratelimit:gemini:rpd:2026-11-20`, `--scan`, `DBSIZE`, heartbeat TTLs | `0`; only the 2 heartbeats remain (TTL 11 s, still refreshed by the workers); DBSIZE 2 |
| Other DBs | `INFO keyspace` | only db0 present |

- No FLUSHDB, FLUSHALL, pattern deletion or other key change.
- Limiter thresholds unchanged (RPM 4 / TPM 200000 / RPD 16).
- Model unchanged (`gemini-3.8-flash`).
- **Rationale:** the deleted counter recorded usage of the **old** project. The limiter models
  per-project quota, and the new project had made 0 requests through this app.

## 2. The probe (exactly one Gemini request)

- **Path:** production `make_summarize_stage()` → `ask()` → shared `RateLimiter.acquire()` (granted) →
  `GeminiClient.generate()`.
- **Inputs:** new key (from `.env` via Compose, never printed), `gemini-3.8-flash`, thinking `low`,
  timeout 300 s, the stored 6-segment checkpoint of job `9302ffd9` ("Me at the zoo"). Database
  read-only.

Command:

```
docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v <scratchpad>/diag:/diag:ro \
  --entrypoint python worker /diag/gemini_t.py T1
```

Sanitized output (complete):

```
precheck: pacific_day=2026-11-20 rpd_used=0 rpd_limit=16 cooldown_keys=0 model=gemini-3.8-flash thinking=low
          max_output_tokens=8192 timeout_sec=300.0 segments=6 video_id=jNQXAC9IVRw
outcome:  FAILED  code=GEMINI_HTTP_503
          classified_detail={"http_status": 503, "provider_code": "UNAVAILABLE", "retry_after_sec": null}
gemini:   calls=1  sent_at=23:20:47.139Z  answered_at=23:20:48.822Z  duration_sec=1.68
          exception_type=ServerError  http_status=503  google_status=UNAVAILABLE
after:    rpd_used=1
exit 1
```

| Question | Answer |
|---|---|
| Did the request reach Google? | **Yes**: one SDK call; Google returned an HTTP response (`ServerError` 503) |
| Result | **HTTP 503, Google status `UNAVAILABLE`**, after 1.68 s; no Retry-After |
| Summarization | **Failed** (no output, no token usage) |
| Retried? | **No** (the script caps at one request; no rerun) |
| Limiter | granted the permit; RPD counter 0 → 1 |
| Secrets | 0 key-like strings in the output; no `.env` content shown |

## 3. Interpretation

- With the **old project** (2026-11-20, 15 requests) and now a **different project** (this probe),
  `gemini-3.8-flash` answers **503 `UNAVAILABLE`**, including for this tiny 6-segment request that had
  succeeded on 2026-11-20.
- So the failure is **not specific to the old key or project**, nor to our request size, timeout or
  code: valid requests reach Google, and Google reports the model as unavailable.
- The most plausible cause is **unavailability or overload of `gemini-3.8-flash` on the Gemini API**
  (free tier). That is external; it cannot be confirmed from our side beyond Google's own status.

## 4. Not done

- No retry.
- No job: Step 2 (the user's Take Notes click) was not requested because the probe failed.
- No YouTube or Notion calls.
- No model change: a different model needs the user's approval.
- No Git operations.
- Root `handover.md` untouched.

## 5. Recommended next actions (user decision)

1. **Wait and probe once more later** (same single probe).
2. **Or approve a model change.** Probe one other free-tier Flash model available to the new key (check
   its RPM/TPM/RPD in AI Studio first and set `GEMINI_*_LIMIT` accordingly). If it answers, set
   `GEMINI_MODEL` (`.env`, user) and recreate the workers. This is a configuration change only; the code
   already reads `GEMINI_MODEL`.

## 6. Files changed

- `docs/PROJECT_STATUS_AND_HANDOVER.md`: the key/project change, the reset, the probe result, the
  updated blocker and next steps.
- This log.
