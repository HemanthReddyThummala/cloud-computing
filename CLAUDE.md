# Project instructions for Claude Code sessions

## Project status — single source of truth
- **`docs/PROJECT_STATUS_AND_HANDOVER.md` is the authoritative project-status document.** Read it at the
  start of a session; do not trust older session logs over it.
- **At the end of every session that changes implementation, tests, deployment or blockers, update
  that file:** verified changes, exact test results, deployment state (deployed image vs repository),
  blockers and remaining work. Skip it if nothing changed.
- **Never mark something done because code exists.** Distinguish verified against real services /
  tested with fakes / blocked / not implemented, and label anything you could not check as
  **Unverified**.
- **Do not duplicate project status in other files.** The root `handover.md` is an unmaintained
  leftover; do not update it.

## Session logs
- Every session writes its own new Markdown log in `docs/session_logs/`. Never overwrite an existing
  log.
- Record the status-document update in that log.

## Git and secrets
- Never stage, commit, push, change Git identity, or add co-author trailers; the user handles Git.
- Never print, log or copy `.env` values, API keys, OAuth tokens or other secrets. Do not modify `.env`.

## Useful commands
- Backend tests: `docker compose --profile test build tests`, then
  `MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -q`.
- Extension tests: `cd extension && npm test` (no build step; load `extension/` unpacked in Chrome).
- Deploy backend (only with user approval): `docker compose build migrate`, then
  `docker compose up -d --no-build gateway outbox-publisher worker`. Never `down -v`.
