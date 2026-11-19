"""Drain-safe, re-runnable work-queue migration with a broker-enforced write block (Option B).

Real RabbitMQ 4.4.1 + real management API. Management/deletion *failures* are simulated by
wrapping the real client or injecting a failing delete; everything else is the real broker.
"""
import asyncio
import os
import time

import aio_pika
import pytest
from aio_pika import Message
from aio_pika.exceptions import DeliveryError

from notesagent.common.broker import LEGACY_QUEUE_WORK, QUEUE_WORK
from notesagent.common.config import get_settings
from notesagent.ops.rabbitmq_mgmt import ManagementApi, ManagementRequestRejected, ManagementUnavailable
from notesagent.ops.work_queue_migration import (
    EXIT_COMPLETE,
    EXIT_DRAINING,
    POLICY_APPLY_TO,
    POLICY_DEFINITION,
    POLICY_NAME,
    POLICY_PATTERN,
    POLICY_PRIORITY,
    MigrationRefused,
    main,
    migrate,
)
from tests.support.legacy import bound_routing_keys, mgmt_api, setup_broker, wait_for_stats
from tests.support.rabbit import queue_state

SETTLE = 6.0   # must exceed the broker's ~5 s statistics interval (same default as the script)
URL = os.environ["TEST_RABBITMQ_URL"]


def mgmt_args():
    return os.environ["TEST_RABBITMQ_MGMT_URL"], URL


def run(mgmt=None, **kwargs):
    kwargs.setdefault("settle_sec", SETTLE)
    return asyncio.run(migrate(get_settings(), mgmt or mgmt_api(), **kwargs))


def our_policy():
    return mgmt_api().policy(POLICY_NAME)


def apply_our_policy():
    mgmt_api().put_policy(POLICY_NAME, pattern=POLICY_PATTERN, definition=POLICY_DEFINITION,
                          priority=POLICY_PRIORITY, apply_to=POLICY_APPLY_TO)


def assert_final_topology():
    assert not queue_state(LEGACY_QUEUE_WORK).exists
    assert queue_state(QUEUE_WORK).exists
    assert bound_routing_keys(QUEUE_WORK) == ["job.work"]
    assert our_policy() is None                                             # removed after deletion


def assert_legacy_kept(*, bound: bool, policy: bool):
    assert queue_state(LEGACY_QUEUE_WORK).exists
    assert bound_routing_keys(LEGACY_QUEUE_WORK) == (["job.work"] if bound else [])
    assert (our_policy() is not None) == policy


async def _publish_direct_to_legacy() -> str:
    conn = await aio_pika.connect(URL)
    async with conn:
        ch = await conn.channel(publisher_confirms=True)
        try:
            await ch.default_exchange.publish(Message(b"late"), routing_key=LEGACY_QUEUE_WORK)
            return "accepted"
        except DeliveryError:
            return "rejected"


# ---------------------------------------------------------------- happy paths and reruns
def test_fresh_install_without_legacy_queue():
    report = run()
    assert report.exit_code == EXIT_COMPLETE and "legacy queue absent" in report.outcome
    assert_final_topology()
    rerun = run()
    assert rerun.exit_code == EXIT_COMPLETE and any("already present" in s for s in rerun.steps)


def test_full_migration_from_phase1_state_then_reruns_are_noops():
    setup_broker(legacy=True, legacy_bound=True)
    wait_for_stats(LEGACY_QUEUE_WORK)
    report = run()
    assert report.exit_code == EXIT_COMPLETE, report
    steps = " | ".join(report.steps)
    for expected in ("declared", f"{LEGACY_QUEUE_WORK}: removed", "policy " + POLICY_NAME + ": applied",
                     "verified effective", "write-blocked; deleted", f"policy {POLICY_NAME}: removed"):
        assert expected in steps, (expected, steps)
    # order: policy applied before deletion; removed only after deletion
    applied = report.steps.index(f"policy {POLICY_NAME}: applied (max-length=0, overflow=reject-publish)")
    deleted = next(i for i, s in enumerate(report.steps) if s.endswith("write-blocked; deleted"))
    removed = report.steps.index(f"policy {POLICY_NAME}: removed")
    assert applied < deleted < removed
    assert_final_topology()
    for _ in range(2):                                                      # completion is derived from broker state
        again = run()
        assert again.exit_code == EXIT_COMPLETE and f"policy {POLICY_NAME}: absent" in " ".join(again.steps)


@pytest.mark.parametrize("state", ["after_declare_v2", "after_bind_v2", "after_unbind_legacy"])
def test_resume_after_interruption_before_policy(state):
    setup_broker(legacy=True, legacy_bound=(state != "after_unbind_legacy"),
                 v2=True, v2_bound=(state != "after_declare_v2"))
    wait_for_stats(LEGACY_QUEUE_WORK)
    report = run()
    assert report.exit_code == EXIT_COMPLETE, report
    assert any("already present" in s or "already removed" in s for s in report.steps)
    assert_final_topology()


def test_resume_after_interruption_with_policy_applied():
    setup_broker(legacy=True, legacy_bound=False, v2=True, v2_bound=True)
    apply_our_policy()
    wait_for_stats(LEGACY_QUEUE_WORK, lambda d: d.get("policy") == POLICY_NAME)
    report = run()
    assert report.exit_code == EXIT_COMPLETE, report
    assert f"policy {POLICY_NAME}: already applied" in report.steps
    assert_final_topology()


def test_resume_after_deletion_but_before_policy_removal():
    setup_broker(legacy=False, v2=True, v2_bound=True)
    apply_our_policy()                                                      # left behind by an interrupted run
    report = run()
    assert report.exit_code == EXIT_COMPLETE, report
    assert f"{LEGACY_QUEUE_WORK}: absent" in report.steps and f"policy {POLICY_NAME}: removed" in report.steps
    assert_final_topology()


def test_check_mode_changes_nothing():
    setup_broker(legacy=True, legacy_bound=True)
    wait_for_stats(LEGACY_QUEUE_WORK)
    report = run(check_only=True)
    assert report.exit_code == EXIT_COMPLETE and "would" in " ".join(report.steps)
    assert not queue_state(QUEUE_WORK).exists
    assert_legacy_kept(bound=True, policy=False)


# ---------------------------------------------------------------- the broker-enforced write block itself
def test_write_block_rejects_new_publishes_but_keeps_draining_messages(procs, fx):
    """Evidence on RabbitMQ 4.4.1: with the policy, the broker nacks publishes to jobs.work,
    existing messages stay (not dropped), and an unacked message returned by a crashed holder
    is NOT dropped either."""
    setup_broker(legacy=True, legacy_bound=True, messages=2)
    wait_for_stats(LEGACY_QUEUE_WORK, lambda d: d["messages_ready"] == 2)
    marker = fx / "held"
    holder = procs.start("holder", "tests.support.hold_message", LEGACY_QUEUE_WORK, marker)
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)

    assert asyncio.run(_publish_direct_to_legacy()) == "accepted"          # before the policy
    report = run()                                                          # unbinds + applies policy, then waits
    assert report.exit_code == EXIT_DRAINING, report
    assert asyncio.run(_publish_direct_to_legacy()) == "rejected"          # broker-enforced block
    assert queue_state(LEGACY_QUEUE_WORK).messages == 2                    # 1 original ready + 1 early 'late'

    procs.sigkill(holder.pid)                                               # unacked message returns...
    wait_until_ready(3)                                                     # ...and is not dropped
    assert_legacy_kept(bound=False, policy=True)


def test_policy_targets_only_the_legacy_queue(rabbit_admin):
    setup_broker(legacy=True, legacy_bound=True)

    async def _lookalikes():
        conn = await aio_pika.connect(URL)
        async with conn:
            ch = await conn.channel()
            for name in ("jobs.work.x", "xjobs.work", "jobs.workers"):
                await ch.declare_queue(name, durable=True)
    asyncio.run(_lookalikes())
    wait_for_stats(LEGACY_QUEUE_WORK)
    report = run(wait_sec=0, settle_sec=0.1)
    assert f"policy {POLICY_NAME}: verified effective on {LEGACY_QUEUE_WORK} only" in report.steps
    for name in (QUEUE_WORK, "jobs.work.x", "xjobs.work", "jobs.workers"):
        assert rabbit_admin.queue_details(name).get("policy") in (None, ""), name


# ---------------------------------------------------------------- deletion refusals (policy retained)
def wait_until_ready(n, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue_state(LEGACY_QUEUE_WORK).messages == n:
            return
        time.sleep(0.2)
    raise AssertionError(f"legacy queue did not reach {n} ready messages")


def test_refuses_with_ready_messages_then_completes_after_drain():
    setup_broker(legacy=True, legacy_bound=True, messages=2)
    wait_for_stats(LEGACY_QUEUE_WORK, lambda d: d["messages_ready"] == 2)
    report = run()
    assert report.exit_code == EXIT_DRAINING and "ready=2" in report.steps[-1]
    assert_legacy_kept(bound=False, policy=True)
    assert queue_state(LEGACY_QUEUE_WORK).messages == 2                    # policy dropped nothing

    async def _drain():                                                     # what workers do
        conn = await aio_pika.connect(URL)
        async with conn:
            q = await (await conn.channel()).declare_queue(LEGACY_QUEUE_WORK, passive=True)
            while (m := await q.get(no_ack=False, fail=False)) is not None:
                await m.ack()
    asyncio.run(_drain())
    final = run(wait_sec=30)                                                # stats lag -> waits, re-queries
    assert final.exit_code == EXIT_COMPLETE, final
    assert_final_topology()


def test_refuses_with_unacknowledged_message_even_if_first_sample_is_stale(procs, fx):
    setup_broker(legacy=True, legacy_bound=False, messages=1)
    wait_for_stats(LEGACY_QUEUE_WORK, lambda d: d["messages_ready"] == 1)
    marker = fx / "held"
    holder = procs.start("holder", "tests.support.hold_message", LEGACY_QUEUE_WORK, marker)
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    report = run()                                                          # immediately: stats may still be stale
    assert report.exit_code == EXIT_DRAINING, report
    assert_legacy_kept(bound=False, policy=True)
    procs.sigkill(holder.pid)
    wait_until_ready(1)
    assert run().exit_code == EXIT_DRAINING


def test_refuses_while_a_consumer_is_attached():
    setup_broker(legacy=True, legacy_bound=True)

    async def _with_consumer():
        conn = await aio_pika.connect(URL)
        async with conn:
            q = await (await conn.channel()).declare_queue(LEGACY_QUEUE_WORK, passive=True)
            await q.consume(lambda m: None)
            wait_for_stats(LEGACY_QUEUE_WORK, lambda d: d["consumers"] == 1)
            return await migrate(get_settings(), mgmt_api(), settle_sec=SETTLE)
    report = asyncio.run(_with_consumer())
    assert report.exit_code == EXIT_DRAINING and "consumers=1" in report.steps[-1]
    assert_legacy_kept(bound=False, policy=True)


def test_deletion_failure_keeps_queue_and_policy_then_rerun_completes():
    setup_broker(legacy=True, legacy_bound=True)
    wait_for_stats(LEGACY_QUEUE_WORK)

    async def _failing_delete(channel):
        raise RuntimeError("simulated broker failure during queue.delete")
    with pytest.raises(MigrationRefused, match="deleting jobs.work failed.*policy were kept"):
        run(delete_queue=_failing_delete)
    assert_legacy_kept(bound=False, policy=True)
    report = run()
    assert report.exit_code == EXIT_COMPLETE, report
    assert_final_topology()


# ---------------------------------------------------------------- policy failures
class _PolicyWriteRejected(ManagementApi):
    def put_policy(self, *args, **kwargs):
        raise ManagementRequestRejected("management API rejected PUT (HTTP 400): simulated")


def test_policy_application_failure_deletes_nothing():
    setup_broker(legacy=True, legacy_bound=True)
    wait_for_stats(LEGACY_QUEUE_WORK)
    with pytest.raises(MigrationRefused, match="rejected PUT"):
        run(_PolicyWriteRejected(*mgmt_args()))
    assert_legacy_kept(bound=False, policy=False)
    assert run().exit_code == EXIT_COMPLETE                                 # healthy rerun resumes
    assert_final_topology()


class _PolicyNeverEffective(ManagementApi):
    def queue(self, name):
        doc = super().queue(name)
        if doc and name == LEGACY_QUEUE_WORK:
            doc["policy"] = None
            doc["effective_policy_definition"] = {}
        return doc


def test_policy_verification_failure_deletes_nothing_and_keeps_policy():
    setup_broker(legacy=True, legacy_bound=True)
    wait_for_stats(LEGACY_QUEUE_WORK)
    with pytest.raises(MigrationRefused, match="not confirmed effective"):
        run(_PolicyNeverEffective(*mgmt_args()), policy_verify_sec=2)
    assert_legacy_kept(bound=False, policy=True)
    assert run().exit_code == EXIT_COMPLETE
    assert_final_topology()


def test_existing_policy_with_same_name_but_different_definition_is_not_overwritten():
    setup_broker(legacy=True, legacy_bound=True)
    mgmt_api().put_policy(POLICY_NAME, pattern=POLICY_PATTERN, definition={"max-length": 5},
                          priority=POLICY_PRIORITY, apply_to=POLICY_APPLY_TO)
    with pytest.raises(MigrationRefused, match="refusing to overwrite"):
        run()
    assert our_policy()["definition"] == {"max-length": 5}                 # untouched
    assert queue_state(LEGACY_QUEUE_WORK).exists


def test_other_policy_matching_legacy_queue_is_not_overridden():
    setup_broker(legacy=True, legacy_bound=True)
    mgmt_api().put_policy("someone-elses-policy", pattern="^jobs\\.", definition={"max-length": 1000},
                          priority=0, apply_to="queues")
    with pytest.raises(MigrationRefused, match="other policies already match"):
        run()
    assert our_policy() is None and queue_state(LEGACY_QUEUE_WORK).exists


# ---------------------------------------------------------------- management API unavailable (fail closed)
def test_unreachable_management_api_changes_nothing():
    setup_broker(legacy=True, legacy_bound=True)
    with pytest.raises(MigrationRefused, match="fail closed"):
        run(ManagementApi("http://rabbitmq:1", URL))
    assert not queue_state(QUEUE_WORK).exists
    assert_legacy_kept(bound=True, policy=False)


class _IncompleteStats(ManagementApi):
    def queue(self, name):
        doc = super().queue(name)
        if doc and name == LEGACY_QUEUE_WORK:
            doc.pop("messages_unacknowledged", None)
        return doc


def test_incomplete_statistics_refuse_deletion_and_keep_policy():
    setup_broker(legacy=True, legacy_bound=True)
    wait_for_stats(LEGACY_QUEUE_WORK)
    with pytest.raises(MigrationRefused, match="incomplete statistics"):
        run(_IncompleteStats(*mgmt_args()))
    assert_legacy_kept(bound=False, policy=True)


class _FailsRightBeforeDeletion(ManagementApi):
    """Healthy until the re-query immediately before deletion. Reads of the legacy queue that
    already show the policy effective are: (1) verification, (2) first sample, (3) second
    sample right before deletion -> fails there."""
    def __init__(self, *args):
        super().__init__(*args)
        self.effective_reads = 0

    def queue(self, name):
        doc = super().queue(name)
        if name == LEGACY_QUEUE_WORK and doc and doc.get("policy") == POLICY_NAME:
            self.effective_reads += 1
            if self.effective_reads >= 3:
                raise ManagementUnavailable("management API unreachable (simulated)")
        return doc


def test_management_failure_right_before_deletion_then_rerun_completes():
    setup_broker(legacy=True, legacy_bound=True)
    wait_for_stats(LEGACY_QUEUE_WORK)
    with pytest.raises(MigrationRefused, match="fail closed"):
        run(_FailsRightBeforeDeletion(*mgmt_args()))
    assert_legacy_kept(bound=False, policy=True)                            # policy never removed early
    report = run()
    assert report.exit_code == EXIT_COMPLETE, report
    assert f"policy {POLICY_NAME}: already applied" in report.steps
    assert_final_topology()


class _PolicyRemovalFails(ManagementApi):
    def delete_policy(self, name):
        raise ManagementUnavailable("management API unreachable (simulated)")


def test_policy_cleanup_failure_after_deletion_is_finished_by_rerun():
    setup_broker(legacy=True, legacy_bound=True)
    wait_for_stats(LEGACY_QUEUE_WORK)
    with pytest.raises(MigrationRefused, match="fail closed"):
        run(_PolicyRemovalFails(*mgmt_args()))
    assert not queue_state(LEGACY_QUEUE_WORK).exists and our_policy() is not None
    report = run()
    assert report.exit_code == EXIT_COMPLETE and f"policy {POLICY_NAME}: removed" in report.steps
    assert_final_topology()


def test_v2_with_unexpected_arguments_is_never_modified(rabbit_admin):
    setup_broker(legacy=True, legacy_bound=True)

    async def _declare_wrong_v2():
        conn = await aio_pika.connect(URL)
        async with conn:
            await (await conn.channel()).declare_queue(QUEUE_WORK, durable=True, arguments={"x-queue-type": "quorum"})
    asyncio.run(_declare_wrong_v2())
    with pytest.raises(MigrationRefused, match="expected"):
        run()
    assert rabbit_admin.queue_details(QUEUE_WORK)["arguments"] == {"x-queue-type": "quorum"}
    assert_legacy_kept(bound=True, policy=False)


# ---------------------------------------------------------------- CLI
def test_cli_exit_codes_and_no_credentials_in_output(capsys):
    setup_broker(legacy=True, legacy_bound=True, messages=1)
    wait_for_stats(LEGACY_QUEUE_WORK, lambda d: d["messages_ready"] == 1)
    assert main(["--settle", str(SETTLE)]) == EXIT_DRAINING
    out = capsys.readouterr().out
    assert "WAITING:" in out and "ready=1" in out
    password = URL.split("@")[0].rsplit(":", 1)[-1]
    assert password not in out and password not in repr(mgmt_api())
