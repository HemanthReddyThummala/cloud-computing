"""Drain-safe, re-runnable migration from the Phase 1 work queue to topology v2 (Option B).

    python -m notesagent.ops.work_queue_migration [--check] [--wait SECONDS] [--settle SECONDS]

Why a policy: RabbitMQ 4.4.1 quorum queues do NOT support queue.delete with if-empty /
if-unused (NOT_IMPLEMENTED over AMQP and the management API — verified). Instead of a
delete-time guard, the broker is made to *reject every publish* to the legacy queue before it
is verified empty and deleted, so nothing can enter it between the last check and deletion.

Sequence — each step first inspects the live broker, so a re-run after an interruption at ANY
point resumes or merely verifies; nothing is recreated, overwritten or removed blindly:

  1. Ensure ``jobs.work.v2`` exists with the expected arguments (refuse if they differ).
  2. Ensure the binding  jobs --job.work--> jobs.work.v2.
  3. If the legacy ``jobs.work`` is absent: clean up our write-block policy if it is still
     present (interrupted after deletion) and finish.
  4. Unbind  jobs --job.work--> jobs.work.
  5. Apply the temporary write-block policy  ``notes-agent-legacy-work-write-block``
     (pattern ^jobs\\.work$, definition {max-length: 0, overflow: reject-publish}, priority 1000,
     apply-to queues). Refuse — never overwrite — if a policy with that name but a different
     definition exists, or if any other policy matches jobs.work.
  6. Verify through the management API that the policy is effective on jobs.work and that it
     targets ONLY jobs.work.
  7. Verify the queue is empty and idle: two management samples >= --settle s apart (stats are
     refreshed every ~5 s) BOTH show ready=0, unacknowledged=0, total=0, consumers=0, then a
     real-time AMQP passive declare shows 0 ready messages.
  8. Delete jobs.work (plain delete — the policy is the guard). On failure: keep the policy.
  9. Only after confirmed deletion, remove the policy and verify it is gone.

Any management-API problem at any point => refuse (fail closed); the policy, if applied, is
kept until the queue is gone. Messages already in the queue are never dropped by the policy
(reject-publish only affects new publishes; verified), and workers keep draining it.

Exit codes: 0 = complete (or --check found nothing unsafe), 1 = refused/error,
3 = legacy queue not yet empty/idle (draining) — re-run later or use --wait.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aio_pika
from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel
from aio_pika.exceptions import ChannelNotFoundEntity

from notesagent.common.broker import (
    EXCHANGE_DLX,
    EXCHANGE_JOBS,
    LEGACY_QUEUE_WORK,
    QUEUE_WORK,
    ROUTING_KEY_WORK,
    work_queue_arguments,
)
from notesagent.common.config import Settings, get_settings
from notesagent.common.logging import setup_logging
from notesagent.ops.rabbitmq_mgmt import ManagementApi, ManagementUnavailable

log = logging.getLogger(__name__)

EXIT_COMPLETE, EXIT_REFUSED, EXIT_DRAINING = 0, 1, 3
_STAT_FIELDS = ("messages_ready", "messages_unacknowledged", "messages", "consumers")

POLICY_NAME = "notes-agent-legacy-work-write-block"
POLICY_PATTERN = r"^jobs\.work$"
POLICY_DEFINITION: dict[str, Any] = {"max-length": 0, "overflow": "reject-publish"}
POLICY_PRIORITY = 1000
POLICY_APPLY_TO = "queues"
_QUEUE_POLICY_TARGETS = {"queues", "all", "classic_queues", "quorum_queues"}

DeleteQueue = Callable[[AbstractChannel], Awaitable[Any]]


class MigrationRefused(Exception):
    pass


@dataclass
class QueueStats:
    ready: int
    unacked: int
    total: int
    consumers: int

    @property
    def idle_and_empty(self) -> bool:
        return self.ready == 0 and self.unacked == 0 and self.total == 0 and self.consumers == 0

    def describe(self) -> str:
        return (f"ready={self.ready}, unacknowledged={self.unacked}, total={self.total}, "
                f"consumers={self.consumers}")


@dataclass
class Report:
    steps: list[str] = field(default_factory=list)
    outcome: str = ""
    exit_code: int = EXIT_COMPLETE

    def step(self, text: str) -> None:
        self.steps.append(text)
        log.info("migration step", extra={"step": text})

    def finish(self, outcome: str, exit_code: int) -> Report:
        self.outcome, self.exit_code = outcome, exit_code
        return self


def stats_from(document: dict[str, Any], name: str) -> QueueStats:
    missing = [f for f in _STAT_FIELDS if not isinstance(document.get(f), int) or isinstance(document.get(f), bool)]
    if missing:
        raise MigrationRefused(f"management API returned incomplete statistics for {name} "
                               f"(missing: {', '.join(missing)}); refusing to decide on deletion")
    return QueueStats(document["messages_ready"], document["messages_unacknowledged"],
                      document["messages"], document["consumers"])


def is_our_policy(document: dict[str, Any] | None) -> bool:
    return bool(document) and (
        document.get("pattern") == POLICY_PATTERN
        and document.get("definition") == POLICY_DEFINITION
        and document.get("priority") == POLICY_PRIORITY
        and document.get("apply-to") == POLICY_APPLY_TO)


def _has_work_binding(bindings: list[dict[str, Any]]) -> bool:
    return any(b.get("routing_key") == ROUTING_KEY_WORK for b in bindings)


def _mgmt(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except ManagementUnavailable as exc:
        raise MigrationRefused(f"{exc}; refusing to continue (fail closed)") from None


async def _passive_ready_count(connection, name: str) -> int | None:
    channel = await connection.channel()
    try:
        queue = await channel.declare_queue(name, passive=True)
        return queue.declaration_result.message_count
    except ChannelNotFoundEntity:
        return None
    finally:
        try:
            await channel.close()
        except Exception:  # noqa: BLE001
            pass


async def _default_delete(channel: AbstractChannel) -> Any:
    return await channel.queue_delete(LEGACY_QUEUE_WORK)


# ---------------------------------------------------------------- write-block policy
def _ensure_write_block(mgmt: ManagementApi, report: Report, check_only: bool) -> bool:
    """Apply the policy if needed. Returns False in check mode when it would have been applied."""
    existing = _mgmt(mgmt.policy, POLICY_NAME)
    if existing is not None and not is_our_policy(existing):
        raise MigrationRefused(f"a policy named {POLICY_NAME!r} already exists with a different definition "
                               f"({existing.get('pattern')!r}, {existing.get('definition')}); refusing to overwrite it")
    others = [p.get("name") for p in _mgmt(mgmt.policies)
              if p.get("name") != POLICY_NAME and p.get("apply-to") in _QUEUE_POLICY_TARGETS
              and _pattern_matches(p.get("pattern", ""), LEGACY_QUEUE_WORK)]
    if others:
        raise MigrationRefused(f"other policies already match {LEGACY_QUEUE_WORK}: {others}; refusing to override "
                               "them with the write block")
    if existing is not None:
        report.step(f"policy {POLICY_NAME}: already applied")
        return True
    if check_only:
        report.step(f"policy {POLICY_NAME}: absent (would be applied)")
        return False
    _mgmt(mgmt.put_policy, POLICY_NAME, pattern=POLICY_PATTERN, definition=POLICY_DEFINITION,
          priority=POLICY_PRIORITY, apply_to=POLICY_APPLY_TO)
    report.step(f"policy {POLICY_NAME}: applied (max-length=0, overflow=reject-publish)")
    return True


def _pattern_matches(pattern: str, name: str) -> bool:
    try:
        return re.search(pattern, name) is not None
    except re.error:
        return True  # cannot evaluate -> treat as matching (refuse rather than guess)


async def _verify_write_block(mgmt: ManagementApi, report: Report, verify_sec: float) -> None:
    deadline = time.monotonic() + verify_sec
    while True:
        document = _mgmt(mgmt.queue, LEGACY_QUEUE_WORK)
        if document is None:
            return  # queue already gone; nothing to protect
        if document.get("policy") == POLICY_NAME and document.get("effective_policy_definition") == POLICY_DEFINITION:
            break
        if time.monotonic() >= deadline:
            raise MigrationRefused(f"policy {POLICY_NAME} is not confirmed effective on {LEGACY_QUEUE_WORK} "
                                   f"(policy={document.get('policy')!r}); nothing was deleted, policy kept")
        await asyncio.sleep(0.5)
    affected = sorted(q.get("name") for q in _mgmt(mgmt.queues) if q.get("policy") == POLICY_NAME)
    if affected != [LEGACY_QUEUE_WORK]:
        raise MigrationRefused(f"policy {POLICY_NAME} applies to {affected}, not only {LEGACY_QUEUE_WORK}; "
                               "nothing was deleted, policy kept")
    report.step(f"policy {POLICY_NAME}: verified effective on {LEGACY_QUEUE_WORK} only")


def _remove_write_block(mgmt: ManagementApi, report: Report, check_only: bool) -> None:
    """Called ONLY when the legacy queue is confirmed absent."""
    existing = _mgmt(mgmt.policy, POLICY_NAME)
    if existing is None:
        report.step(f"policy {POLICY_NAME}: absent")
        return
    if not is_our_policy(existing):
        raise MigrationRefused(f"policy {POLICY_NAME!r} has an unexpected definition; not removing it")
    if check_only:
        report.step(f"policy {POLICY_NAME}: present after deletion (would be removed)")
        return
    _mgmt(mgmt.delete_policy, POLICY_NAME)
    if _mgmt(mgmt.policy, POLICY_NAME) is not None:
        raise MigrationRefused(f"policy {POLICY_NAME} still present after removal; re-run to finish cleanup")
    report.step(f"policy {POLICY_NAME}: removed")


# ---------------------------------------------------------------- migration
async def migrate(settings: Settings, mgmt: ManagementApi, *, check_only: bool = False, wait_sec: float = 0.0,
                  settle_sec: float = 6.0, poll_sec: float = 2.0, policy_verify_sec: float = 15.0,
                  delete_queue: DeleteQueue = _default_delete) -> Report:
    report = Report()
    expected_args = work_queue_arguments(settings)
    complete = "complete: legacy queue absent, v2 present and bound, write-block policy removed"

    v2 = _mgmt(mgmt.queue, QUEUE_WORK)          # fail closed before touching anything
    connection = await aio_pika.connect(settings.rabbitmq_url.get_secret_value(), timeout=10,
                                        client_properties={"connection_name": "work-queue-migration"})
    try:
        channel = await connection.channel()

        # ---- 1. jobs.work.v2 with the expected arguments
        if v2 is None:
            if check_only:
                report.step(f"{QUEUE_WORK}: absent (would be declared)")
            else:
                await channel.declare_exchange(EXCHANGE_JOBS, ExchangeType.DIRECT, durable=True)
                await channel.declare_exchange(EXCHANGE_DLX, ExchangeType.FANOUT, durable=True)
                await channel.declare_queue(QUEUE_WORK, durable=True, arguments=expected_args)
                report.step(f"{QUEUE_WORK}: declared")
        else:
            actual = v2.get("arguments") or {}
            if actual != expected_args:
                raise MigrationRefused(f"{QUEUE_WORK} exists with arguments {actual} but {expected_args} "
                                       "were expected; refusing to modify it")
            report.step(f"{QUEUE_WORK}: already present (arguments verified)")

        # ---- 2. binding jobs --job.work--> v2
        if _has_work_binding(_mgmt(mgmt.bindings, EXCHANGE_JOBS, QUEUE_WORK)):
            report.step(f"binding {EXCHANGE_JOBS} -> {QUEUE_WORK}: already present")
        elif check_only:
            report.step(f"binding {EXCHANGE_JOBS} -> {QUEUE_WORK}: absent (would be created)")
        else:
            jobs = await channel.declare_exchange(EXCHANGE_JOBS, ExchangeType.DIRECT, durable=True)
            queue = await channel.declare_queue(QUEUE_WORK, durable=True, arguments=expected_args)
            await queue.bind(jobs, ROUTING_KEY_WORK)
            report.step(f"binding {EXCHANGE_JOBS} -> {QUEUE_WORK}: created")

        # ---- 3. legacy queue already gone -> only policy cleanup remains
        if _mgmt(mgmt.queue, LEGACY_QUEUE_WORK) is None:
            report.step(f"{LEGACY_QUEUE_WORK}: absent")
            _remove_write_block(mgmt, report, check_only)
            return report.finish(complete, EXIT_COMPLETE)

        # ---- 4. unbind legacy
        if _has_work_binding(_mgmt(mgmt.bindings, EXCHANGE_JOBS, LEGACY_QUEUE_WORK)):
            if check_only:
                report.step(f"binding {EXCHANGE_JOBS} -> {LEGACY_QUEUE_WORK}: present (would be removed)")
            else:
                jobs = await channel.declare_exchange(EXCHANGE_JOBS, ExchangeType.DIRECT, durable=True)
                legacy_queue = await channel.get_queue(LEGACY_QUEUE_WORK, ensure=True)
                await legacy_queue.unbind(jobs, ROUTING_KEY_WORK)
                report.step(f"binding {EXCHANGE_JOBS} -> {LEGACY_QUEUE_WORK}: removed")
        else:
            report.step(f"binding {EXCHANGE_JOBS} -> {LEGACY_QUEUE_WORK}: already removed")

        # ---- 5 + 6. broker-enforced write block, verified
        if _ensure_write_block(mgmt, report, check_only):
            await _verify_write_block(mgmt, report, policy_verify_sec)

        # ---- 7. verified empty and idle (two samples + live count), then 8. delete
        deadline = time.monotonic() + wait_sec
        while True:
            document = _mgmt(mgmt.queue, LEGACY_QUEUE_WORK)
            if document is None:
                report.step(f"{LEGACY_QUEUE_WORK}: already deleted")
                _remove_write_block(mgmt, report, check_only)
                return report.finish(complete, EXIT_COMPLETE)
            first = stats_from(document, LEGACY_QUEUE_WORK)
            if first.idle_and_empty:
                await asyncio.sleep(settle_sec)                                # outlive stale statistics
                document = _mgmt(mgmt.queue, LEGACY_QUEUE_WORK)                # re-query right before deletion
                if document is None:
                    report.step(f"{LEGACY_QUEUE_WORK}: already deleted")
                    _remove_write_block(mgmt, report, check_only)
                    return report.finish(complete, EXIT_COMPLETE)
                second = stats_from(document, LEGACY_QUEUE_WORK)
                live_ready = await _passive_ready_count(connection, LEGACY_QUEUE_WORK)
                if second.idle_and_empty and live_ready == 0:
                    if check_only:
                        report.step(f"{LEGACY_QUEUE_WORK}: verified empty and idle (would be deleted)")
                        return report.finish("check: safe to proceed", EXIT_COMPLETE)
                    try:
                        await delete_queue(channel)
                    except Exception as exc:  # noqa: BLE001
                        raise MigrationRefused(f"deleting {LEGACY_QUEUE_WORK} failed ({type(exc).__name__}: "
                                               f"{str(exc)[:160]}); the queue and the write-block policy were "
                                               "kept; re-run to retry") from None
                    if await _passive_ready_count(connection, LEGACY_QUEUE_WORK) is not None:
                        raise MigrationRefused(f"{LEGACY_QUEUE_WORK} still exists after deletion; policy kept; re-run")
                    report.step(f"{LEGACY_QUEUE_WORK}: verified empty twice ({settle_sec:g}s apart) and live, "
                                "write-blocked; deleted")
                    # ---- 9. only now remove the policy
                    _remove_write_block(mgmt, report, check_only)
                    return report.finish(complete, EXIT_COMPLETE)
                reason = (f"live AMQP count reports {live_ready} ready messages" if second.idle_and_empty
                          else second.describe())
            else:
                reason = first.describe()
            if time.monotonic() >= deadline:
                report.step(f"{LEGACY_QUEUE_WORK}: not deleted — {reason} (write-block policy stays applied)")
                return report.finish("draining: legacy queue is not yet verified empty and idle; "
                                     "keep workers running and re-run", EXIT_DRAINING)
            await asyncio.sleep(poll_sec)
    finally:
        await connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drain-safe migration of the work queue to jobs.work.v2")
    parser.add_argument("--check", action="store_true", help="report what would happen; change nothing")
    parser.add_argument("--wait", type=float, default=0.0, help="seconds to keep waiting for the legacy queue to drain")
    parser.add_argument("--settle", type=float, default=6.0,
                        help="seconds between the two emptiness samples (must exceed the stats interval, ~5 s)")
    args = parser.parse_args(argv)

    settings = get_settings(service_name="work-queue-migration")
    setup_logging("work-queue-migration", "WARNING")
    mgmt = ManagementApi(settings.rabbitmq_management_url, settings.rabbitmq_url.get_secret_value())
    try:
        report = asyncio.run(migrate(settings, mgmt, check_only=args.check, wait_sec=args.wait,
                                     settle_sec=args.settle))
    except MigrationRefused as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    for step in report.steps:
        print(f"  - {step}")
    print(("OK: " if report.exit_code == EXIT_COMPLETE else "WAITING: ") + report.outcome)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
