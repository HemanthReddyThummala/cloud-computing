# Controlled Gemini diagnostic tests T1 / T2

Date: 2026-11-20 20:25 UTC (2026-11-21 01:55 IST). Plan: `gemini_integration_root_cause_diagnosis.md` §7.

**Result: T1 FAILED (HTTP 503 `UNAVAILABLE`), so T2 was NOT run (stop rule).**

## 1. Test conditions

- **Isolation:**
  - throwaway container: `docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v <scratchpad>/diag:/diag:ro
    --entrypoint python worker /diag/gemini_t.py T1`;
  - deployed image `notes-agent-backend:dev` = `00ef0f4` (`70dce0098cd2`) with the worker's Compose
    environment (real key from `.env`, never printed);
  - running services not touched; no rebuild or restart.
- **Code under test:** the **production** `worker/summarize.py:make_summarize_stage()` → `summarize()`
  → `ask()` with the **production** `worker/gemini.py:GeminiClient` and the shared
  `common/ratelimit.py:RateLimiter` on the dev Redis, so the quota is counted correctly.
- **Instrumentation (in the throwaway process only, script in the scratchpad, not in the repo):**
  - a wrapper around the SDK's `aio.models.generate_content` with a **hard cap of 1 request**;
  - records the time sent / answered, duration, HTTP status, exception type, and Google `status`
    **only if it matches `^[A-Z][A-Z0-9_]{0,63}$`**;
  - on success it would also record usage metadata, including thinking tokens;
  - **no** message bodies, headers, key or transcript text are output.
- **Data:** the TRANSCRIBING checkpoint was read **read-only** (`conn.read_only = True`); nothing was
  written to the database; no job was created; no gateway, YouTube or Notion call.
- **Pre-checks (printed by the script before any request):**
  - Pacific day 2026-11-20, RPD used **11/16**, 0 cooldown keys;
  - model `gemini-3.8-flash`, thinking `low`, `max_output_tokens` 8192;
  - T1 timeout 120 s (production default).
  - Safety rule: stop if RPD ≥ limit − 1 or a cooldown exists. Passed.

## 2. Runs

| Run | What happened | Gemini requests |
|---|---|---|
| T1, try 0 (20:25:33 UTC) | `ModuleNotFoundError: notesagent`: a script run by path does not put `/app` on `sys.path`. Failed at import, **before** any limiter or Gemini call | **0** |
| **T1** (20:25:43 UTC) | ran; result below | **1** |
| **T2** | **SKIPPED**: T1 failed (stop rule) | 0 |

### T1 result (6-segment "Me at the zoo" checkpoint of job `9302ffd9-…`, video `jNQXAC9IVRw`)

```
precheck: pacific_day=2026-11-20 rpd_used=11/16 cooldown_keys=0 model=gemini-3.8-flash thinking=low
          max_output_tokens=8192 timeout_sec=120 segments=6
outcome:  FAILED  code=GEMINI_HTTP_503
          classified_detail={"http_status": 503, "provider_code": "UNAVAILABLE"}
gemini:   calls=1  sent_at=2026-11-20T20:25:45.177Z  answered_at=2026-11-20T20:25:49.465Z
          duration_sec=4.29  exception_type=ServerError  http_status=503  google_status=UNAVAILABLE
after:    rpd_used=12
```

- **Token counts:** not available (no successful response; usage metadata exists only on success).
- **Output scan:** 0 key-like strings (`AIza…`, `x-goog-api-key`, `api_key`).
- **Afterwards:**
  - all services running and healthy;
  - `/healthz` 200;
  - jobs 4, checkpoints 3 (unchanged);
  - no leftover one-off container;
  - Redis RPD for 2026-11-20 = **12** (+1, the T1 request).

## 3. Conclusions

1. **This is the exact input that succeeded through the same code, SDK (3.6.1), `v1` endpoint, model,
   schema and thinking level on 2026-11-20 (Phase B Part 2: OK in 32.7 s).** It now gets **503
   `UNAVAILABLE` in 4.3 s.** So the 503s are **not** caused by:
   - the transcript length, content or format of the real video;
   - the request weight;
   - the 120 s deadline;
   - the retry logic;
   - anything specific to the jobs.

   **H2 (request weight) is refuted as the cause of the 503s.** It may still matter for the 504s,
   which happen at our 120 s deadline.
2. **Google's own status is now known: `UNAVAILABLE`.** This is Google's code for "the service/model is
   temporarily unavailable (overloaded), try again later". Not quota (`RESOURCE_EXHAUSTED`/429), not
   auth (`PERMISSION_DENIED`/`UNAUTHENTICATED`), not the model ID (`NOT_FOUND`), not the request
   (`INVALID_ARGUMENT`).
3. **Root cause (confirmed for this moment): `gemini-3.8-flash` is unavailable for requests from our API
   key/project through `generativelanguage.googleapis.com/v1`.** It is a provider-side availability
   problem for the API path, even for a minimal request.
4. **Why AI Studio still answered:** the AI Studio UI is served through a different route or capacity
   pool than API-key requests. The UI working does not prove that the API is available for this
   model. *(Hypothesis consistent with all evidence; not provable from our side.)*
5. **Not our code:** the request reaches Google, is accepted as valid (no 400), and is answered with
   `UNAVAILABLE`. The integration, limiter, classification and retries behave correctly. The code
   defects from the previous diagnosis remain valid as robustness and observability issues, but they
   do not cause the failure:
   - C1: 120 s deadline → 504;
   - C2: provider status not persisted;
   - C3: a short retry window that burns quota;
   - C4: popup wording.

## 4. Recommended minimal fix / next steps (not implemented)

1. **No code change will make Gemini answer while it reports `UNAVAILABLE`.** Wait and retry later,
   and do not resubmit blindly: every failed job costs up to 5 of 16 daily requests (4 left until
   08:00 UTC on 2026-11-21).
2. **Before the next real job, one probe request** (exactly T1 again, 1 RPD), and only submit through
   the extension if it returns OK.
3. **Smallest code/config changes worth making regardless (on approval):**
   - **C2 (observability):** persist the sanitized `provider_code` (`UNAVAILABLE`, `DEADLINE_EXCEEDED`,
     …) in the `retry_scheduled` / `dead_lettered` events and the worker warning log. This test showed
     the value is available and safe;
   - **C3 (resilience):** a longer backoff for Gemini 503/504 (for example 60 / 180 / 600 s), so short
     outages are ridden out with fewer wasted requests;
   - **C1:** pass `GEMINI_REQUEST_TIMEOUT_SEC` through Compose (about 300 s) to avoid 504s at our own
     deadline when the model is slow;
   - **C4:** popup text for `GEMINI_HTTP_5xx`.
4. **Optional operational alternative (user decision, configuration only):** if `gemini-3.8-flash`
   stays unavailable via the API, test another free-tier Flash model that AI Studio lists for the same
   key with the T1 probe. Then set `GEMINI_MODEL` accordingly; the code already reads it. Its rate
   limits must be checked in AI Studio first, because ADR-13's limits are per model.

## 5. Not done

- T2 (skipped by the stop rule).
- No retry of T1, no job submission, no Notion or YouTube call.
- No change to source, `.env`, Compose, database or handover.
- No Git operation.
