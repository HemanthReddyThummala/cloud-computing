# Feasibility check: switching SUMMARIZING to "Gemini 4 Hydrogen" (no API request made)

Date: 2026-11-21 IST.

Context:
- `gemini-3.8-flash` returns 503 `UNAVAILABLE` from two different Google projects
  (`gemini_new_key_reset_and_probe.md`).
- The user found "Gemini 4 Hydrogen" in AI Studio, showing RPM 5 / TPM 250,000 / RPD 20 and usage 0.

**No Gemini API request was made.** No `.env`, code, Redis, limiter or container change. Sources were
public documentation (web pages, not API calls) and a read-only, network-less inspection of our image.

## 1. Sources

- **Official Google docs** (`ai.google.dev/gemini-api/docs/*` now redirect to
  `aistudio.google.com/docs/*`):
  - `models`, `models/gemini-4-hydrogen`, `models/gemini-3.8-flash`, `whats-new-gemini-4-hydrogen`;
  - `rate-limits`, `thinking`, `structured-output` (partly summarised), `changelog` (partly
    summarised), `deprecations`.
- **Third-party pages (lower weight):** eesel.ai, teamorouter.com, apidog.com, kie.ai,
  systemdesign.academy, freellm.net, tecadmin.net, nhimg.org, emergent.sh.
- **Our code:** `worker/gemini.py`, `common/config.py`, `common/ratelimit.py`. The installed
  `google-genai` was inspected in image `notes-agent-backend:13a9334` with `--network none`.

## 2. Findings

| # | Question | Finding | Confidence |
|---|---|---|---|
| 1 | Exact API model ID | **`gemini-4-hydrogen`** (official models table: "Gemini 4 Hydrogen \| `gemini-4-hydrogen`"; model page "Stable: `gemini-4-hydrogen`"). No preview/dated/latest alias. **GA since 2026-11-12**, on the free tier (official changelog). No shutdown date | High (official) |
| 1b | Relation to `gemini-3.8-flash` | Official docs: 3.8-flash is GA (2026-09-02) with no shutdown date. Hydrogen is "the recommended upgrade from Gemini 3.8 Flash"; migration = "Change your model string from `gemini-3.8-flash` to `gemini-4-hydrogen`". **No redirect.** A third-party claim that Hydrogen was tested "as gemini-3.8-flash" is **not** supported by the official docs | High (official) |
| 2 | Accessible to our key/project | **Unverified.** AI Studio lists it for the user's project with usage 0, which suggests access. Proof needs an API call (the approved probe would show it) | Medium |
| 3a | SDK `google-genai==3.6.1` | No model allowlist (any string passes); `GenerateContentConfig` has `response_json_schema`, `system_instruction`, `max_output_tokens`, `thinking_config`; the SDK also has an `interactions` API. Our `GEMINI_MODEL` validator accepts `gemini-4-hydrogen`. **The switch is configuration-only** (`GEMINI_MODEL`), with no code change | High (inspected) |
| 3b | Endpoint used by our code | SDK default `v1` → `POST /v1/models/{model}:generateContent`. **Official Hydrogen examples only use the Interactions API** (`/v1beta/interactions`). The docs do not say whether `v1 generateContent` serves Hydrogen | **Uncertain**: the main risk |
| 3c | Structured JSON output | Hydrogen supports "Structured outputs" (model page badge). The docs show it via the Interactions `response_format`. `responseJsonSchema` on generateContent for Hydrogen is **not explicitly documented** | Medium |
| 3d | System instruction | Not explicitly confirmed for Hydrogen in the pages read | Medium |
| 4 | Thinking | Hydrogen: thinking on by default (`high`); levels **low, medium, high, xhigh**; `thinking_level` is the control; **`low` is valid** (our setting). `minimal` is rejected (our validator still allows `minimal`, but we use `low`). `thinking_budget` is not the Gemini 4 control (we do not send it) | High (official) |
| 4b | Unsupported parameters | Hydrogen rejects out-of-range `temperature`/`top_p`/`top_k` and has no explicit caching. **Our code sends none of these** (only system instruction, JSON mime type + schema, `max_output_tokens` 8192, thinking level) | High |
| 4c | Token limits | 1,048,576 context; 65,536 max output. Our ≈ 4.7k-token prompt and `max_output_tokens` 8192 are well inside | High (official) |
| 5 | Rate limits | The official rate-limits page publishes **no** free-tier numbers for Hydrogen: "can be viewed in Google AI Studio". **Limits apply per project, not per API key**; RPD resets at midnight Pacific. The AI Studio values the user saw (5 / 250,000 / 20) are the authoritative ones for this project and model; they cannot be confirmed from the docs | AI Studio = authoritative; docs silent |

## 3. Limiter configuration

- **ADR-13 convention:** `GEMINI_*_LIMIT` values are enforced **as given** and already include a **× 0.8
  safety margin** (the current 4 / 200,000 / 16 came from AI Studio's 5 / 250,000 / 20 for
  `gemini-3.8-flash`).
- For Hydrogen's displayed **5 / 250,000 / 20**:
  - **with the project's margin (recommended): RPM 4, TPM 200,000, RPD 16**. These are **identical to the
    current values**, so no limiter change is needed;
  - **without a margin (exactly as displayed): RPM 5, TPM 250,000, RPD 20.** This allows hitting Google's
    real limit (a 429) under bursts, contrary to R9 ("never trigger an LLM 429").
- **Counter note:** our Redis Gemini counters are model-agnostic (one `rpd:<Pacific day>` key for all
  Gemini usage), while Google counts per model per project. The new project's counter currently holds 1,
  from the 3.8-flash probe, so after a switch it would over-count Hydrogen usage by 1 (conservative,
  harmless). No reset is needed.

## 4. Uncertainties and the proposed single probe (needs approval)

**Main uncertainty:** whether `POST /v1/models/gemini-4-hydrogen:generateContent` (our SDK path) serves the
model, with `responseJsonSchema` + system instruction + `thinkingLevel: LOW`.

**Proposed probe** (exactly 1 request):
- the same throwaway-container script and 6-segment input as before;
- the model is overridden **only in that container**: `-e GEMINI_MODEL=gemini-4-hydrogen`;
- no `.env` change, no worker restart; the shared limiter is used; the timeout stays 300 s.

| Result | Meaning | Next |
|---|---|---|
| 200 + valid notes | full compatibility via `v1 generateContent` | user sets `GEMINI_MODEL=gemini-4-hydrogen` in `.env` → recreate the workers → Take Notes once |
| 404 `NOT_FOUND` | not served on `v1` generateContent (or not available to the project) | possible config-only fallback: `GOOGLE_GENAI_API_VERSION=v1beta` (SDK-supported). That needs approval and a second probe |
| 400 `INVALID_ARGUMENT` | some parameter unsupported (likely `responseJsonSchema` or the system instruction) | inspect the sanitized status; a small code change may be needed |
| 503 | also unavailable | external; stop |
| 429 | Google quota (≥ 1 call with an HTTP status) vs local limiter (0 calls) | distinguish and stop |

## 5. Files changed

- `docs/PROJECT_STATUS_AND_HANDOVER.md`: a §5 note on the candidate model and its open questions.
- This log.

No Git operations. Root `handover.md` untouched.
