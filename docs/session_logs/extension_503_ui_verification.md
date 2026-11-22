# Extension: 503 / temporary-service-error UI verification (inspection only)

Date: 2026-11-22 IST.

Scope: inspection and the existing tests only.

Not done:
- no job, no click, no browser automation;
- no external YouTube, Gemini or Notion call;
- no change to code, tests, configuration, Redis, database or containers;
- no 503 fabricated;
- no `.env` access; no Git;
- root `handover.md` untouched.

## 1. Files inspected

- `extension/lib/popup-view.js`: view models (`reasonText`, `errorText`, `describeJob`).
- `extension/lib/jobs.js`: `sanitizeStatus`, the polling tracker (`pollOne`), `MAX_JOBS = 10`.
- `extension/lib/gateway.js`: `callGateway` maps HTTP errors to `ExtensionError(code)`.
- `extension/popup.js`: renders `describeJob(...)` headline and details.
- `extension/manifest.json`: name "Video Notes Agent", version 0.1.0.
- `backend/notesagent/gateway/routes.py` and `backend/notesagent/worker/retry_policy.py`
  (`public_retry_view`, the terminal error document).
- Tests: `extension/tests/manifest_and_popup.test.js`, `extension/tests/jobs.test.js`.

## 2. Code path: a Gemini 503 from Google to the popup

1. **Worker:** `classify_gemini_error` turns a real 503 into a retryable `GEMINI_HTTP_503`
   (`provider_code UNAVAILABLE`, ADR-21). The retry policy schedules a retry (60 s, 180 s, 600 s…) and
   stores a retry document.
   - **A 503 is never a direct `FAILED`.** The job either retries and completes, or after
     `MAX_ATTEMPTS` becomes `DEAD_LETTERED` with `error.code RETRIES_EXHAUSTED` and
     `last_error_code GEMINI_HTTP_503` (+ `last_provider_code UNAVAILABLE` since ADR-21).
2. **Gateway `GET /jobs/{id}`:**
   - `retry` = `public_retry_view` → `{reason, attempt, max_attempts, next_attempt_at}`. The
     `provider_code` is not exposed while waiting.
   - `error` = the terminal document.
3. **Extension `sanitizeStatus`:** keeps `retry.{reason, attempt, maxAttempts, nextAttemptAt}` and
   `error.{code, message, lastErrorCode, resetsAt, lastProviderCode}`. Only an UPPER_CASE
   `last_provider_code` is kept.
4. **Polling:** `pollOne` polls every 3 s while active and every **10 s while `retry` is present**
   (`retryWaitMs`). It stops at a terminal status.
5. **Popup (`describeJob`):**
   - **Waiting** (non-terminal + `retry`): the tone is `wait`, the headline stays on the current
     stage (e.g. "Summarizing…"), and the detail reads **"Waiting to retry: the AI model is temporarily
     unavailable (overloaded) (attempt N of M). Still processing."**
   - **`DEAD_LETTERED` after 503s:** the headline is **"Failed after retries"**, the failed step is
     marked, and the detail reads **"Processing kept failing and was stopped after several attempts.
     Last cause: the AI model is temporarily unavailable (overloaded). Please try again later."**
     ("Please try again later" is added for any `*_HTTP_5xx` last cause.)
   - 504 → "took too long to respond"; other 5xx → "had a temporary server problem".
   - No raw codes or JSON are shown.
6. **A 503 from the gateway itself** (e.g. `DATABASE_UNAVAILABLE` on a status poll):
   - `callGateway` throws `ExtensionError(code)`;
   - `pollOne` marks `connection: "error"` and backs off (≤ 30 s);
   - the popup shows **"Status check failed; still trying."**, the job is not marked failed, and
     polling continues.

## 3. Tests

`cd extension && npm test` → **94 passed, 0 failed** (9 suites, about 1.9 s).

| Scenario | Covered? | Test |
|---|---|---|
| Waiting after a Gemini 503 → "still processing" with the reason and attempt | ✅ | `manifest_and_popup.test.js` "popup: a job waiting after a Gemini 503 is shown as still processing with the reason" (`retry {reason GEMINI_HTTP_503, attempt 2, maxAttempts 5}` → exact text) |
| `DEAD_LETTERED` with last cause 503 / `UNAVAILABLE` → clear text + "Please try again later", no raw code or JSON | ✅ | "popup: Gemini 503 / UNAVAILABLE after exhausted retries is clear and suggests a later retry" |
| 504, other 5xx, provider code only, `NOTION_HTTP_503` | ✅ | "popup: Gemini 504, other 5xx, provider code only, and non-5xx causes" |
| `reasonText` mapping | ✅ | "popup: reasonText maps provider HTTP codes" |
| Sanitizing the gateway's `last_error_code` / `last_provider_code` | ✅ | `jobs.test.js` (around line 392) |
| Slower polling while `retry` is present; gateway trouble → "still trying" | ✅ | `jobs.test.js` "a job waiting for a retry is polled more slowly…" (with a `NOTION_RATE_LIMITED` reason); "popup job: waiting for a retry and gateway trouble are shown as still processing" |

**Gap (minor):** no single test feeds a **snake_case gateway response with
`retry.reason = "GEMINI_HTTP_503"`** through `sanitizeStatus` **and** `describeJob`. The two halves are
tested separately (sanitizing a generic `retry`; rendering `GEMINI_HTTP_503`).

## 4. Additional read-only check with real data (no external call)

The two stored jobs dead-lettered by real Gemini 503s were run through the extension's own code in
Node: `acffab62` and `f87c737a` (2026-11-20, before ADR-21, so without `last_provider_code`). Each was
fetched with `GET http://localhost:8000/jobs/{id}`, then passed to `sanitizeStatus` and
`describeJob`. Result for both:

- gateway: `DEAD_LETTERED`, `error {code RETRIES_EXHAUSTED, attempts 5, last_error_code GEMINI_HTTP_503}`, `retry null`;
- popup: headline **"Failed after retries"**, tone `error`, failed step **Summarizing**;
- detail: **"Processing kept failing and was stopped after several attempts. Last cause: the AI model
  is temporarily unavailable (overloaded). Please try again later."**

This verifies the terminal 503 display end to end with real gateway responses, outside Chrome. The
**waiting** variant could not be checked with real data: no job is waiting now, and creating one or
faking a 503 is excluded.

## 5. UI findings

- ✅ A temporary upstream 503 is **not** presented as a permanent failure. While retrying, the popup
  says "Still processing". Only after the retry budget is used up does it say "Failed after retries",
  and then it names the temporary cause and invites a later retry.
- ✅ The messages are understandable and contain no raw codes or JSON.
- ⚠ **Retry timing is not shown.** `nextAttemptAt` is sanitized and stored but not displayed, so
  during a 60–600 s Gemini backoff the user sees no countdown. This is accurate, just incomplete.
- ⚠ **"(attempt N of M)" is slightly ambiguous.** `attempt` is the number of failed executions so far.
  After the first 503 the popup says "attempt 1 of 5" while the next run is the 2nd execution. It is
  consistent with the backend, but it could be read as "currently on attempt 1".
- ⚠ **Minor inconsistency:** the waiting view does not receive `provider_code` (`public_retry_view`
  omits it), so a non-503 code with provider status `UNAVAILABLE` reads "had a temporary problem"
  while waiting but "temporarily unavailable" when terminal. This doesn't affect `GEMINI_HTTP_503`
  itself, which is matched by code.
- ℹ **Chrome:** the popup keeps only the **10 most recent** tracked jobs (`MAX_JOBS`). The stored 503
  jobs from 2026-11-20 have very likely been evicted, and today's 503-affected jobs (`6649512b`,
  `b3a24d11`, `c35f1c13`) all completed. So the 503 wording **cannot currently be seen in Chrome**; it
  appears only while a real 503 retry is in progress.

## 6. Unresolved

- Real-Chrome rendering of either 503 message: **not observed**.
- Optional improvements (code changes need approval):
  - a chained `sanitizeStatus` → `describeJob` test with a `GEMINI_HTTP_503` retry;
  - showing the next retry time;
  - clearer attempt wording.

## 6a. User-reported Chrome check (after this inspection)

- The user reloaded the unpacked extension in Chrome and reported: **"popup shows no errors"**.
- This is user-reported and was not independently observed by Claude.
- It confirms that the current extension code loads and the popup renders without errors after the
  reload.
- It does **not** show the 503 wording in Chrome: no 503 job is in the popup's 10-job history (see
  §5).

## 7. Status document

**Updated** only with the verified fact: real terminal-503 gateway responses render correctly through
the extension's code (Node), and extension tests pass 94/94 on 2026-11-22. Chrome rendering stays
"not yet seen".
