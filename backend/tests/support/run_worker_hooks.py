"""TEST-ONLY: the real JobWorker with fixture stages plus fault hooks at exact crash windows.

Env:
  HOOK_CRASH_AFTER      schedule_retry | dead_letter  -> SIGKILL right after that transaction COMMITS
                        (before the message is acked / the mailbox deleted)
  HOOK_DB_FAIL_RETRY_TX N  -> the first N retry transactions raise OperationalError (nothing committed)
  HOOK_DB_FAIL_LOCK     N  -> the first N lock acquisitions raise PoolTimeout (PostgreSQL unreachable)
  HOOK_DELAY_PUBLISH_FAIL N -> the first N delay-queue republishes raise (broker publish failure)
  HOOK_AUDIT_CRASH / HOOK_AUDIT_DB_FAIL -> dead-letter auditor hooks (see below)
Each hook writes a marker file into FIXTURE_DIR when it fires.
"""
import os
import signal
from pathlib import Path

import psycopg
from psycopg_pool import PoolTimeout

from notesagent.common import jobs_repo
from notesagent.common.broker import BrokerClient
from notesagent.worker import dlq_audit, runner
from notesagent.worker.__main__ import serve
from tests.support.fixture_stages import build_fixture_stages

FX = Path(os.environ["FIXTURE_DIR"])
_counts = {"retry_tx": 0, "lock": 0, "publish": 0}


def _mark(name: str) -> None:
    (FX / f"hook.{name}.{os.getpid()}.{sum(_counts.values())}").write_text("")


def _crash_after(name, original):
    async def wrapper(*args, **kwargs):
        result = await original(*args, **kwargs)          # transaction committed
        if os.environ.get("HOOK_CRASH_AFTER") == name:
            _mark(f"crash_after_{name}")
            os.kill(os.getpid(), signal.SIGKILL)
        return result
    return wrapper


_schedule_retry = jobs_repo.schedule_retry


async def _schedule_retry_maybe_fail(*args, **kwargs):
    limit = int(os.environ.get("HOOK_DB_FAIL_RETRY_TX", "0"))
    if _counts["retry_tx"] < limit:
        _counts["retry_tx"] += 1
        _mark("db_fail_retry_tx")
        raise psycopg.OperationalError("simulated: PostgreSQL unavailable during the retry transaction")
    return await _schedule_retry(*args, **kwargs)


jobs_repo.schedule_retry = _crash_after("schedule_retry", _schedule_retry_maybe_fail)
jobs_repo.dead_letter = _crash_after("dead_letter", jobs_repo.dead_letter)

_handle = runner.JobWorker._handle


async def _handle_maybe_fail(self, message, job_id):
    limit = int(os.environ.get("HOOK_DB_FAIL_LOCK", "0"))
    if _counts["lock"] < limit:
        _counts["lock"] += 1
        _mark("db_fail_lock")
        raise PoolTimeout("simulated: could not obtain a PostgreSQL connection for the advisory lock")
    return await _handle(self, message, job_id)


runner.JobWorker._handle = _handle_maybe_fail

_republish = BrokerClient.republish


async def _republish_maybe_fail(self, message, **kwargs):
    limit = int(os.environ.get("HOOK_DELAY_PUBLISH_FAIL", "0"))
    if _counts["publish"] < limit and kwargs.get("exchange") == "jobs.retry":
        _counts["publish"] += 1
        _mark("delay_publish_fail")
        raise RuntimeError("simulated: broker refused the delayed copy")
    return await _republish(self, message, **kwargs)


BrokerClient.republish = _republish_maybe_fail

# ---------------------------------------------------------------- dead-letter auditor (ADR-12)
#   HOOK_AUDIT_CRASH    before | after -> SIGKILL right before the audit transaction starts / right
#                       after it COMMITS (before the mailbox is deleted and the message acked)
#   HOOK_AUDIT_DB_FAIL  N -> the first N audit transactions raise OperationalError (nothing committed)
_counts["audit_db"] = 0
_apply = dlq_audit.DeadLetterAuditor._apply


async def _apply_with_hooks(self, conn, dl):
    if os.environ.get("HOOK_AUDIT_CRASH") == "before":
        _mark("audit_crash_before")
        os.kill(os.getpid(), signal.SIGKILL)
    if _counts["audit_db"] < int(os.environ.get("HOOK_AUDIT_DB_FAIL", "0")):
        _counts["audit_db"] += 1
        _mark("audit_db_fail")
        raise psycopg.OperationalError("simulated: PostgreSQL unavailable during the audit transaction")
    outcome = await _apply(self, conn, dl)
    if os.environ.get("HOOK_AUDIT_CRASH") == "after":
        _mark("audit_crash_after")
        os.kill(os.getpid(), signal.SIGKILL)
    return outcome


dlq_audit.DeadLetterAuditor._apply = _apply_with_hooks

if __name__ == "__main__":
    serve(build_fixture_stages())
