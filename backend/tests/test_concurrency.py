"""Compare-and-set transitions and the per-job advisory lock."""
import asyncio
import os
import time
import uuid

import psycopg
from psycopg.rows import dict_row

from notesagent.common import jobs_repo
from notesagent.common.errors import StaleJobState
from notesagent.common.states import JobStatus as S
from tests.support.helpers import (
    drain_outbox,
    events,
    run_count,
    started_pids,
    release,
    submit_ok,
    wait_for_status,
    wait_until,
)
from tests.support.rabbit import publish_job_message, queue_state


def test_only_one_concurrent_transition_wins(api, db):
    job_id = uuid.UUID(submit_ok(api))

    async def _race():
        url = os.environ["TEST_DATABASE_URL"]
        c1 = await psycopg.AsyncConnection.connect(url, autocommit=True, row_factory=dict_row)
        c2 = await psycopg.AsyncConnection.connect(url, autocommit=True, row_factory=dict_row)
        async with c1, c2:
            return await asyncio.gather(
                jobs_repo.transition(c1, job_id, expected=S.QUEUED, new=S.TRANSCRIBING, worker_id="w1",
                                     event_type="claimed"),
                jobs_repo.transition(c2, job_id, expected=S.QUEUED, new=S.TRANSCRIBING, worker_id="w2",
                                     event_type="claimed"),
                return_exceptions=True)

    results = asyncio.run(_race())
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, StaleJobState)]
    assert len(winners) == 1 and len(losers) == 1
    assert [e["event_type"] for e in events(db, str(job_id))].count("claimed") == 1


def test_two_workers_never_process_the_same_job_simultaneously(api, db, procs, fx):
    for name in ("worker-a", "worker-b"):
        procs.start(name, "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_GATE="TRANSCRIBING")
    for name in ("worker-a", "worker-b"):
        procs.wait_for_log(name, "worker started")

    job_id = submit_ok(api)
    drain_outbox()
    event_id = str(db.execute("SELECT event_id FROM outbox WHERE job_id = %s", (job_id,)).fetchone()["event_id"])
    publish_job_message(job_id, event_id)                   # duplicate delivery of the same job

    wait_until(lambda: started_pids(fx, job_id, "TRANSCRIBING"), what="stage started")
    time.sleep(3)                                           # the duplicate is offered repeatedly meanwhile
    assert len(started_pids(fx, job_id, "TRANSCRIBING")) == 1, "two workers entered the same stage"
    assert any(e["event_type"] == "duplicate_deferred" for e in events(db, job_id))

    release(fx, job_id)
    wait_for_status(db, job_id, {"COMPLETED"})
    for stage in ("TRANSCRIBING", "SUMMARIZING", "EXTRACTING_FRAMES", "PUBLISHING"):
        assert run_count(fx, job_id, stage) == 1, stage
    assert [e["event_type"] for e in events(db, job_id)].count("claimed") == 1
    wait_until(lambda: queue_state("jobs.work").messages == 0, timeout=15, what="duplicate drained")
