# Final pre-submission audit (read-only)

Date: 2026-11-22 IST. Branch `phase-2` (HEAD `14811c4`). This was a read-only review; this log is
the only file created.

Not done:
- no other file modified;
- nothing staged, committed or pushed; no Git identity change;
- no secrets displayed (pattern scans print only counts and locations; matched values were masked);
- root `handover.md` untouched.

## 1. Working-tree inventory (38 paths: 11 modified, 27 new; 0 staged)

| Category | Files |
|---|---|
| **ADR-22 demo fault switch (code)** | new `backend/notesagent/worker/demo_faults.py`, `backend/notesagent/ops/demo_faults.py`; modified `worker/frames.py`, `worker/runner.py`, `worker/stages.py`, `worker/summarize.py`, `worker/transcribe.py` |
| **ADR-23 limiter namespace (code)** | modified `common/ratelimit.py`, `common/config.py` (both ADRs), `worker/runner.py` (startup log) |
| **Configuration** | modified `docker-compose.yml` (+4: worker-only `GEMINI_LIMITER_NAMESPACE`, `DEMO_FAULTS_ENABLED`), `.env.example` (unused `CHAOS_ENABLED` → `DEMO_FAULTS_ENABLED=false`; `GEMINI_LIMITER_NAMESPACE=` documented) |
| **Tests** | new `backend/tests/test_unit_demo_faults.py` (13), `test_demo_faults.py` (8), `test_limiter_namespace.py` (28) |
| **Documentation** | new `README.md`, `docs/FAILURE_DEMONSTRATION_RUNBOOK.md`; modified `docs/PROJECT_STATUS_AND_HANDOVER.md`, `docs/decisions.md` (ADR-22, ADR-23) |
| **Session logs** | 21 new files in `docs/session_logs/` (plus this audit log = 22) |

Tracked diff: 11 files, +356 / −51 lines. No staged changes.

## 2. Accidental changes, debugging code, artefacts

- **Code diff** (backend, compose, `.env.example`) reviewed line by line. Every change belongs to
  ADR-22 or ADR-23; no unrelated modifications.
- **Debug patterns:**
  - no `breakpoint`, `pdb`, `console.log`, `TODO`, `FIXME`, `XXX` or `HACK` in the added code;
  - the only `print(` calls are the operator output of the demo CLI `ops/demo_faults.py` (intended).
- **Untracked artefacts:** none (`*.pyc`, logs, backups, `__pycache__`, `node_modules`, scratch/diag:
  none).
- **Ignored paths:** `.env`, `.secrets/`, `backend/test-artifacts/` (correctly ignored). `.gitignore`
  covers `.env` and `.env.*` (except `.env.example`), so `.env.gemini-alt` would be ignored too.
- **Unchanged:** `docs/api.md`, `CLAUDE.md` (tracked, unchanged), the whole `extension/` directory.
- **No stale `CHAOS_ENABLED` reader** remains (only a historical mention in ADR-22 text).
- **Line endings:** the 4 large docs are consistently CRLF in the Windows working copy (0 mixed lines).
  `.gitattributes` (`* text=auto eol=lf`) stores them as LF, as in HEAD. Git's "CRLF will be replaced
  by LF" warning is expected and harmless.

## 3. Secrets check

Patterns were scanned across the full tracked diff and every untracked file (≈ 257 KB):

| Pattern | Hits |
|---|---|
| Google API key (`AIza…`) | 0 |
| Notion secret (`secret_…`, `ntn_…`) | 0 |
| Bearer token | 0 |
| `sk-…` key | 0 |
| Private key block | 0 |
| 44-char Fernet-like key | 0 |
| `API_KEY` / `SECRET` / `PASSWORD` / `TOKEN` assigned a literal | 0 |
| URL with credentials (`scheme://user:pass@`) | 2, both placeholders: `README.md:73` `postgresql://USER:PASSWORD@…`, `README.md:75` `amqp://USER:PASSWORD@…` |

`.env` is ignored and not staged. **No secret found.**

## 4. Root `handover.md`

No working-tree change; last commit touching it: `13a9334` (2026-11-21 04:32 IST). **Untouched.**

## 5. ADR-22 / ADR-23 consistency (documentation vs. implementation)

**ADR-22:**

| ADR statement | Code |
|---|---|
| Worker-only switch, default false | `config.demo_faults_enabled = False`; compose worker env only |
| Inert when false | runner creates `DemoFaults` only when enabled; hooks check `ctx.demo_faults is not None` |
| CLI arm / disarm / status, refuses when disabled, 15-min arming | `ops/demo_faults.py` (exit 2, `ARM_TTL_SEC` 900) |
| `GETDEL` one-job binding, 2 h TTL | `DemoFaults.bind()` |
| `dlq` at the metadata request | the `transcribe.call(..., demo_hook)` hook |
| `gemini_429_once` after the permit, `SET NX` once | `summarize.ask()` + `before_gemini_request()` |
| `frames_partial` (odd indices) / `frames_all` | `frames.py` + `frame_fails()` |
| Fail-safe on Redis errors | `_safe()` |

All consistent, and all four scenarios were demonstrated live.

**ADR-23:**

| ADR statement | Code |
|---|---|
| Empty = original keys byte-for-byte | `_gemini_name` → `gemini` |
| `alt` → 4 Gemini keys only, including `settle()` TPM | `_gemini_keys()`, `cooldown_key()`, `settle()` |
| Notion/YouTube unchanged | yes |
| Validation regex | `_LIMITER_NAMESPACE` |
| Startup log label only | yes |
| Workers-only compose | yes |
| 28 tests | yes |

All consistent.

## 6. Status document accuracy

- **§3 / §9 reflect the live demonstrations correctly:** scaling, crash recovery, DLQ, partial frames,
  all frames, Gemini 429; latency measured; 3-click burst.
- **Remaining test-only limitations stated correctly:**
  - chunk-and-merge;
  - live RPM pacing above 4/min;
  - a crash during PUBLISHING;
  - YouTube/Notion 429 (5-A).
- **Waiting 503 popup view:** not seen with real data; Chrome rendering of the 503 text not seen.
- **Real Gemini 503 deviations recorded:** the 429 demo and the latency job.
- **Under-claims (not errors):** several §3 rows still say "verified in one real job" or
  "user-reported" (SUMMARIZING, EXTRACTING_FRAMES, PUBLISHING) although many logged jobs since then
  confirm them.
- **Header:** the "Last updated" line names the documentation cleanup, not the later latency, burst
  and 503-UI entries (which are in §3/§10).
- **Deployment state:** the gateway and publisher still run `2a01b00b9603` (documented in §6); the
  workers run `36c573002412` = repository code. The gateway image predates the ADR-22/23 code changes
  in `common/`. Gateway behaviour is unaffected (the new settings are worker-only), but deployed ≠
  repository for those two services. Documented.

## 7. Recorded test results vs. session logs

| Recorded result | Supporting log |
|---|---|
| Backend **951 passed** | `adr23_gemini_limiter_namespace.md` §3 |
| Backend 923 passed | `demo_fault_switch_implementation.md` §3 (+ task output) |
| Extension **94 passed** | `demo_fault_switch_implementation.md`, `extension_503_ui_verification.md` |
| 21 + 28 new tests | the same logs |
| Live demo results | one log each (`dlq_demo_rehearsal_success`, `frames_partial_demo`, `frames_all_demo`, `gemini_429_once_demo`, `crash_recovery_rehearsal_success`, `scaling_rehearsal_success`) |
| Latency and burst | `latency_evaluation_near_cap`, `burst_evaluation` |

All supported. No test was re-run in this audit, and no code has changed since the 951 run.

## 8. Mandatory requirements (R1–R9): incorrectly marked complete?

**None found.**
- **R2** (SPA navigation) is backed by `content.js` (`yt-navigate-finish`, `popstate`) and three
  content tests.
- **R7-A / R7-B** are backed by real jobs.
- **R8 README:** written, but never followed on a clean machine. This is stated in §8.3, not claimed.
- **R9:** latency was measured, and the burst was verified for 3 clicks. Live RPM pacing and the merge
  path are explicitly marked tests-only.

## 9. Missing files or documentation for submission

**None mandatory.** `README.md`, `docker-compose.yml`, `.env.example`, `docs/api.md`,
`docs/decisions.md`, the runbook, the status document and the unpacked `extension/` are all present.

**Optional:**
- refresh the status-document header and the under-claimed §3 rows;
- follow the README from a clean checkout once;
- rebuild the gateway and publisher images so deployed = repository for all services;
- the known harmless limiter `settle()` TTL quirk;
- the popup's next-retry time and attempt wording.

## 10. Conclusion

**Ready to commit.** No blocker: no secrets, no artefacts, no debug code, no accidental changes, and
the documentation is consistent with the code and the logs. The optional documentation refresh can be
done before or after the commit.
