# New Gemini API key — limiter scope check (stopped before any reset or probe)

Date: 2026-11-21 IST.

Task:
1. inspect the Redis Gemini counters and their scope;
2. reset only counters that are safe to reset;
3. run one probe.

**Result:** stopped after step 1, as the instructions require when safety cannot be established.
- No Redis key was changed.
- No probe was run.
- No external call was made.

## 1. Limiter implementation (`backend/notesagent/common/ratelimit.py`)

- Gemini keys (`RateLimiter._gemini_keys`, `cooldown_key`):
  - `ratelimit:cooldown:gemini` (shared Retry-After cooldown);
  - `ratelimit:gemini:rpm` (sliding 60 s request window);
  - `ratelimit:gemini:tpm` (sliding 60 s input-token window);
  - `ratelimit:gemini:rpd:<Pacific day>` (requests per Pacific calendar day).
- **Scope:** fixed, deployment-wide names. They contain **no API-key or project identifier**. The module
  docstring and ADR-13 state that they model Google's **per-project** limits ("Gemini (per Google
  project)"). The thresholds come from the user's AI Studio limits for `gemini-3.8-flash` × 0.8.
- So the counters are **not specific to the old key**. Changing the key does not by itself make them
  obsolete.

## 2. Current state (read-only)

| Item | Value |
|---|---|
| Gemini keys in Redis db 0 | only `ratelimit:gemini:rpd:2026-11-20` = **16** (string). No rpm / tpm / cooldown keys present |
| Other Redis keys | 2 worker heartbeats (nothing else) |
| `.env` `GEMINI_API_KEY` | set; length 39; classic `AIza` prefix (the previous key was 53 characters, non-`AIza`, per the Phase B Part 2 log). Value never printed |
| `.env` last modified | 2026-11-21 03:31 IST (2026-11-20 22:01 UTC) |
| Running workers (created 22:56 UTC) | **both already hold the same key as `.env`** (sha256 equality checked inside Python; only booleans printed) |
| Last real Google request | job `82960179` (`_S-cBzAg0GE`), 21:49–21:52 UTC = **before** the key change → old key |
| Requests with the new key | **0** (every attempt since was refused by the local limiter) |

## 3. Can the new key's project be established safely?

**No, not from this side.**
- Google API keys are opaque; neither the format nor the length identifies the project.
- Asking Google (for example an error's `ErrorInfo.consumer` or a model-listing call) would need an
  extra real API call, which this task does not authorise. It would also not be the "one probe".

## 4. Decision

- Per the task rules: do not reset a project-level daily counter merely because the key changed; reset
  only counters genuinely specific to the old key.
- The only counter (`ratelimit:gemini:rpd:2026-11-20`) is project-level by design, and the project of
  the new key is unknown. **So nothing was reset.**
- The limiter thresholds are untouched.

## 5. What is needed from the user

Confirm in Google AI Studio (API keys page: each key lists its project; or the rate-limit page for the
new key's project) **whether the new key belongs to a different Google Cloud project than the old one**.

- **Different project** → resetting exactly `ratelimit:gemini:rpd:2026-11-20` is justified: that usage
  belonged to the old project, and the new project's usage through this app is 0. Only that one key
  would be deleted (`DEL`, not a pattern or flush). Then run the single probe.
- **Same project** → do not reset. The probe must wait until the limiter itself allows it.

## 6. Commands (read-only)

- Read `ratelimit.py` (key construction and docstring).
- `redis-cli --scan` / `TYPE` / `GET` on `ratelimit:*`; key-family names only.
- `stat .env` (modification time only).
- `docker inspect` (creation time); a Python sha256 equality check between the `.env` value and the
  container environment value, printing only true/false, length and a prefix flag.

## 7. Files changed

- `docs/PROJECT_STATUS_AND_HANDOVER.md`: the key-change finding and the open question.
- This log.

No source, configuration, `.env`, Redis or Docker state change. Root `handover.md` untouched.
