# ADR-23: per-project Gemini limiter namespace (implementation and deployment preparation)

Date: 2026-11-22 IST. The user approved the design:
- setting `GEMINI_LIMITER_NAMESPACE`;
- `alt` for the alternate project;
- empty by default;
- existing counters and keys preserved;
- limits kept at 4 RPM / 200,000 TPM / 16 RPD unless the alternate project's quota is lower.

Not done:
- **not deployed** (the running workers are unchanged);
- `.env` / `.env.gemini-alt` neither read, opened nor modified;
- no API key printed, logged or hashed;
- no Redis change (production);
- no job; no external API call;
- no Git operation;
- root `handover.md` untouched.

## 1. Before (inspection, read-only)

- **Key names:** `KEY_PREFIX = "ratelimit"` was hard-coded. The keys were `ratelimit:gemini:rpm`,
  `ratelimit:gemini:tpm`, `ratelimit:gemini:rpd:<pacific_day>` and `ratelimit:cooldown:gemini`.
- **Coordination:** one atomic Lua script on Redis' clock, shared by both workers.
- **No project dimension** existed and there was no configuration option, so a different key alone
  would have reused the original counters, which were at 16/16.
- **Key loading:** `GEMINI_API_KEY` reaches the workers only (Compose interpolation) and is read once
  at startup.

## 2. Changes

| File | Change |
|---|---|
| `backend/notesagent/common/config.py` | `gemini_limiter_namespace: str = ""`; validator: empty, or 1–32 characters `a-z0-9-` starting and ending alphanumeric (whitespace stripped); anything else → startup error |
| `backend/notesagent/common/ratelimit.py` | `_gemini_name` = `gemini` or `gemini-<ns>`; `cooldown_key()` is now an instance method (Gemini only gets the namespace); `_gemini_keys()` and `settle()`'s TPM key use it. Lua scripts, atomicity and the day calculation unchanged |
| `backend/notesagent/worker/runner.py` | the "worker started" log adds `gemini_limiter_namespace` (the label or `(default)`), never the key |
| `docker-compose.yml` | worker only: `GEMINI_LIMITER_NAMESPACE: ${GEMINI_LIMITER_NAMESPACE:-}` |
| `.env.example` | documented, with an empty value and the layering command |
| `backend/tests/test_limiter_namespace.py` (new) | 28 tests |
| `docs/decisions.md` | ADR-23 |
| `README.md` | variable-table row; "Using a Gemini key from another project (ADR-23)" |
| `docs/PROJECT_STATUS_AND_HANDOVER.md` | header, feature row, tests, §6 deployment, §9, §10 |

An editor diagnostic reported a "duplicate key" in `docker-compose.yml`. A per-service check found no
duplicate environment key, and the dry run (below) validated the file in Compose. It was a false
positive.

## 3. Tests

| Command | Result |
|---|---|
| `docker compose --profile test build tests` | built |
| `pytest tests/test_limiter_namespace.py tests/test_ratelimit.py tests/test_unit_demo_faults.py` | **67 passed**, 0 failed (30 s), first run |
| full backend suite `pytest -p no:warnings -q -rfE --tb=short` (13:57 → 14:11 IST) | **951 passed, 0 failed** (859 s) = 923 + 28 |
| extension | not run (no extension change) |

**What the new tests prove (real Redis, the test database):**
- **Default compatibility:**
  - unset, `""` and `"   "` give exactly the original four key names, and the Notion/YouTube cooldown
    names;
  - an acquire, settle and 429 report write exactly the original keys, and nothing else, in Redis.
- **Namespace isolation:**
  - `alt` renames only the four Gemini keys;
  - with the original RPD at 16/16, `alt` still grants up to its own limit and then refuses; the
    original counter stays 16;
  - RPM and TPM windows are separate, and `settle()` corrects the `alt` TPM window only.
- **Cooldown isolation:**
  - a 429 under `alt` sets `ratelimit:cooldown:gemini-alt` only, and the original Gemini remains
    callable;
  - a quota cooldown on the original doesn't block `alt`;
  - a Notion 429 still sets the shared `ratelimit:cooldown:notion` (not namespaced).
- **Atomic and shared:** 3 independent processes with `alt`, RPD 5, 9 attempts → exactly 5 grants and 4
  `GEMINI_DAILY_LIMIT_REACHED`; the original counter is untouched.
- **Validation:**
  - rejected: `ALT`, `Alt`, `alt!`, `-alt`, `alt-`, `al t`, `alt:x`, `gemini:rpd`, `alt_1`, 33
    characters, `ä`, a newline, `*`;
  - accepted: `alt`, ` alt ` (→ `alt`), `proj-2`, `a`, 32 characters, `9`.

## 4. Deployment preparation (no container changed)

| Step | Result |
|---|---|
| Running workers before and after | `07df16513cda` / `47094d34563a` on `78864607bddd`, healthy; **unchanged** |
| Rollback tag | `docker tag notes-agent-backend:dev notes-agent-backend:pre-adr23-workers` → `78864607bddd` |
| Build | `docker compose build worker` → exit 0 → `notes-agent-backend:dev` = **`36c573002412`** |
| Content | 45/45 `notesagent/**/*.py` identical in content to the working tree. The first raw listing comparison differed only in the hash-listing format; there are 0 CR characters in the image |
| Behaviour (offline, `--network none`, inside the image) | `''` → original four keys; `'alt'` → `gemini-alt` keys; Notion/YouTube cooldowns unchanged; `ALT:x` → `ValidationError`; default setting `''` |
| Dry run | `docker compose --dry-run up -d --no-deps --no-build --scale worker=2 worker` → only worker-1/worker-2 recreated; exit 0 |
| Rendered setting | worker `GEMINI_LIMITER_NAMESPACE=''`; gateway and publisher don't receive it. Compose rendered the config with `.env` interpolation, and only the namespace field was extracted by a filter; no value was displayed and `.env` was not opened |

## 5. Proposed deployment (awaiting approval)

```bash
export MSYS_NO_PATHCONV=1
docker compose --dry-run up -d --no-deps --no-build --scale worker=2 worker    # only worker-1/-2
docker compose up -d --no-deps --no-build --scale worker=2 worker
```

**Verify:**
- both workers healthy on `36c573002412`;
- loaded settings `gemini_limiter_namespace=''`, `demo_faults_enabled=False`, `max_attempts=5`;
- the startup log shows `"gemini_limiter_namespace": "(default)"`;
- `/healthz` ok, with 2 live workers;
- other services unchanged;
- the original counter `ratelimit:gemini:rpd:2026-11-21` is unchanged.

**Rollback:** `docker tag notes-agent-backend:pre-adr23-workers notes-agent-backend:dev`, then the
same `up` command.

**Then, separately approved:**
1. the user creates `.env.gemini-alt`;
2. `docker compose --env-file .env --env-file .env.gemini-alt up -d --no-deps --no-build --scale worker=2 worker`;
3. verify the namespace `alt` in the loaded settings and that the key is set (boolean only);
4. readiness check on `ratelimit:gemini-alt:rpd:<day>`.

**Switch back:** the plain command above.

**Pairing:** the limiter cannot verify that the key and the namespace belong together; the user is
responsible for that pairing (keys are not compared, hashed or displayed).
