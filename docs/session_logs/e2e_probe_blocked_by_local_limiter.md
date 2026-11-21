# Real end-to-end test, Step 1: Gemini probe refused by our own rate limiter

Date: 2026-11-21 04:43–04:46 IST (2026-11-20 23:14 UTC).

**Result:** the single authorised probe ran through the production code path. The **application's
`RateLimiter` refused it locally** with `GEMINI_DAILY_LIMIT_REACHED` (RPD counter 16/16). **No request
reached Google.**

Per the instructions, I stopped:
- no retry;
- no limiter bypass or limit change;
- no model change;
- no job;
- Step 2 was not started.

## 1. Pre-checks

| Check | Result |
|---|---|
| Git | HEAD `13a9334` ("ADR-21"), clean; deployed image `2a01b00b9603` = `13a9334` (hash-verified earlier today) |
| Worker (`notes-agent-worker-1`) | image `2a01b00b9603`; env `GEMINI_REQUEST_TIMEOUT_SEC=300`, `GEMINI_UNAVAILABLE_BACKOFF_SEC=60,180,600`, `GEMINI_MODEL=gemini-3.8-flash`, `GEMINI_THINKING_LEVEL=low` |
| `/healthz` | 200 |
| Probe script | scratchpad `diag/gemini_t.py` (not in the repo); syntax OK |

### Probe-script changes (scratchpad only)
- **T1 uses the deployed timeout** (300 s) instead of an override.
- **The pre-check is now report-only.** It previously stopped at RPD ≥ limit − 1, which was stricter than
  the application. Now the application's own `RateLimiter` decides; it is neither bypassed nor raised.
- **Fixed the cooldown key pattern** used for reporting (`ratelimit:cooldown:gemini`).
- **`QuotaExhausted` is reported as `LIMITER_REFUSED`.** The recorded SDK call count tells a local
  refusal (0 calls) apart from a Google 429 (≥ 1 call with an HTTP status).

## 2. The probe

Command:

```
docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v <scratchpad>/diag:/diag:ro \
  --entrypoint python worker /diag/gemini_t.py T1
```

This ran the production `make_summarize_stage()` → `ask()` → the shared `RateLimiter.acquire()` → (would
call) `GeminiClient.generate()`, on the stored 6-segment checkpoint. The database was read-only.

Output (sanitized, complete):

```
precheck: pacific_day=2026-11-20 rpd_used=16 rpd_limit=16 cooldown_keys=0 model=gemini-3.8-flash
          thinking=low max_output_tokens=8192 timeout_sec=300.0 segments=6 video_id=jNQXAC9IVRw
outcome:  LIMITER_REFUSED  code=GEMINI_DAILY_LIMIT_REACHED  gemini={"calls": 0}
after:    rpd_used=16
exit 2   (23:14:24 → 23:14:26 UTC)
```

| Field | Value | Meaning |
|---|---|---|
| Source of the refusal | **our `RateLimiter`** (Redis Lua RPD check) | local, before the SDK |
| Gemini SDK calls | **0** | no request sent to Google |
| HTTP status / Google status | none | Google was not contacted |
| Response time / tokens | n/a | — |
| RPD counter before/after | 16 / 16 | no request counted |
| Key-like strings in output | 0 | — |

**This is not a Gemini 503/504/429/authentication failure;** nothing can be concluded about Gemini's
availability from it.

## 3. External API calls

None (Gemini 0, YouTube 0, Notion 0).

## 4. Files changed

| File | Change |
|---|---|
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | limitation #2 = probe refused by the local limiter; reset-time wording removed (user instruction); next steps; session history row |
| `docs/session_logs/e2e_probe_blocked_by_local_limiter.md` | this log |

No source, test, configuration, `.env` or Compose change. No service restarted. Root `handover.md`
untouched.

## 5. Unresolved / next

- The probe can only reach Google once the application's limiter allows a Gemini request. The user
  manages that timing.
- Then: the same single probe → on success, the user clicks **Take Notes once** on `D26sUZ6DHNQ` →
  Claude monitors and verifies every stage.
