# One-shot minimal plain-text Gemini probe → no response within 300 s

Date: 2026-11-21, 09:45:20 UTC (15:15 IST). Approved by the user: exactly one real Gemini request.

## 1. Exact configuration (no secrets)

| Item | Value |
|---|---|
| SDK | official `google-genai==3.6.1`, called **directly** (not through `GeminiClient` / `summarize()`, which add schema, system instruction and thinking) |
| Client | `genai.Client(api_key=<.env key>, http_options=HttpOptions(api_version="v1", retry_options=HttpRetryOptions(attempts=1)))`. **No SDK timeout** was set: setting one makes the SDK add an `X-Server-Timeout` header, which would not be minimal. A local `asyncio.wait_for(..., 300 s)` guard bounded the wait instead |
| Request | `client.aio.models.generate_content(model="gemini-3.8-flash", contents="Reply with exactly: Hello")` → `POST /v1/models/gemini-3.8-flash:generateContent` |
| Not included | no config at all: no response schema/MIME type, no system instruction, no thinking config, no `max_output_tokens`, no tools, no production prompt |
| Limiter | the real shared `RateLimiter.acquire(Provider.GEMINI, tokens=8)` before sending (granted); `settle` only on success |
| Safety | SDK retries off; an in-process wrapper blocks any second call; the script ran once |
| Where | throwaway container `docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v <scratchpad>/diag:/diag:ro --entrypoint python worker /diag/minimal_probe.py` (image `2a01b00b9603` = `13a9334`, worker env from `.env`) |

### Key verification before sending (host side; booleans only, nothing secret printed)
- `.env` last modified **2026-11-21 03:31:00**, unchanged since the user confirmed the new key's project.
- `.env` `GEMINI_API_KEY`: present, length 39, `AIza` prefix.
- Both running workers' key **equals** the `.env` key (sha256 equality computed in-process; no hash
  printed). `docker compose run` injects the same `.env`.
- In the container: key present, length 39, `AIza`; model `gemini-3.8-flash`.

## 2. Result (sanitized, complete)

```
key-check:       key_present=true key_length=39 classic_prefix=true model=gemini-3.8-flash
limiter-before:  rpd_used=0 rpd_limit=16 rpm_limit=4 tpm_limit=200000
limiter:         permit_granted=true
outcome:         LOCAL_TIMEOUT_300S  sent=true  sent_at=2026-11-21T09:45:20.700Z
                 endpoint=POST /v1/models/gemini-3.8-flash:generateContent  latency_sec=300.1
after:           sdk_calls=1  rpd_used=1
exit 1   (09:45:18 → 09:50:21 UTC)
```

| Item | Value |
|---|---|
| Request sent? | **The SDK request was issued once** (`sdk_calls=1`). Whether it reached Google's servers **cannot be confirmed**: no HTTP response line was logged, because none arrived |
| HTTP status | **none**: no response within 300 s; the local guard cancelled it |
| Gemini error status / message | none received |
| Latency | ≥ 300.1 s (cancelled) |
| Returned text | none |
| Rate limiter | **allowed** it (permit granted; RPD counter 0 → 1) |
| Request ID / diagnostic metadata | none (no response headers) |
| Secrets | 0 key-like strings in the output |

## 3. Interpretation

**What it proves:**
- Even a **minimal plain-text** request (no schema, no system instruction, no thinking) did **not**
  succeed. So the earlier failures cannot be blamed solely on our production request features (schema,
  thinking, system instruction).
- The rate limiter, key propagation and SDK invocation worked: one permit, one call.

**What it does not prove:**
- It is **not** an HTTP 503. Without an `X-Server-Timeout` header, the request neither got a quick 503
  (as all 17 earlier requests did) nor an answer.
- The cause of the silence could be server-side (the request held without a reply) or **network-level**
  (for example, the connection or TLS handshake stalling: with no SDK timeout set, `httpx2` waits
  indefinitely). This test cannot tell them apart. No transport error was raised within 300 s.
- It says nothing about whether the model works in AI Studio.

**Overall** (with the earlier evidence: 17 × 503/504 across two projects and two models, and an
identical request flipping 200 → 503): the Gemini API is not serving our requests. **No application-side
cause has been found.**

## 4. Unresolved questions / possible next diagnostic (not done; needs approval)

1. **Is basic connectivity from the Docker network to `generativelanguage.googleapis.com:443` healthy?**
   A TCP connect + TLS handshake from the worker container would answer it. It **sends no Gemini API
   request and uses no quota**. This would separate "network stall" from "server not answering" for
   today's result.
2. If connectivity is fine: the remaining cause is server-side. Nothing in our code can change it; the
   options are the user's (wait, or another provider/model decision).

## 5. Files changed

- `docs/PROJECT_STATUS_AND_HANDOVER.md`: §5 result and history.
- This log.
- The probe script lives in the scratchpad only (`diag/minimal_probe.py`), not in the repository.

No production code, `.env`, Compose, container or Redis change (beyond the limiter's own counting of this
one permit). No Git operations. Root `handover.md` untouched.
