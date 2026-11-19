"""Pure retry decisions (ADR-11): budgets, backoff, Retry-After, quota reset, identity, API view."""
from datetime import datetime, timedelta, timezone

import pytest

from notesagent.common.config import Settings
from notesagent.common.errors import (
    FatalStageError,
    QuotaExhausted,
    RateLimited,
    RateLimiterUnavailable,
    TransientError,
)
from notesagent.worker.retry_policy import (
    DeadLetterPlan,
    RetryPlan,
    backoff_step,
    choose_bucket,
    plan_failure,
    public_retry_view,
    seconds_until,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
RESET = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)


def settings(**kw):
    base = dict(database_url="postgresql://u:p@h/d", rabbitmq_url="amqp://u:p@h/", backoff_schedule_sec="10,30,90",
                lock_retry_delay_sec=5, infra_retry_delay_sec=30, max_attempts=5, infra_max_attempts=20)
    return Settings(**{**base, **kw})


def job(attempts=0, infra=0):
    return {"attempts": attempts, "infra_attempts": infra}


T = TransientError("X_TRANSIENT", "t")


# ---------------------------------------------------------------- backoff + buckets
@pytest.mark.parametrize("attempt,expected", [(1, 10), (2, 30), (3, 90), (4, 90), (9, 90)])
def test_backoff_last_step_repeats(attempt, expected):
    assert backoff_step(attempt, [10, 30, 90]) == expected


@pytest.mark.parametrize("remaining,expected", [(10, 10), (9.5, 5), (120, 90), (31, 30), (1, 5), (0.01, 5), (90, 90)])
def test_choose_bucket(remaining, expected):
    assert choose_bucket(remaining, [5, 10, 30, 90]) == expected


# ---------------------------------------------------------------- MAX_ATTEMPTS = total executions (P3-D6)
def test_five_attempts_means_four_retries_then_dead_letter():
    s = settings()
    delays = []
    for failures_so_far in range(4):
        plan = plan_failure(job(failures_so_far), T, s, NOW)
        assert isinstance(plan, RetryPlan) and plan.attempt == failures_so_far + 1
        delays.append(plan.delay_sec)
    assert delays == [10, 30, 90, 90]
    final = plan_failure(job(4), T, s, NOW)
    assert isinstance(final, DeadLetterPlan) and final.attempt == 5
    assert final.error == {"code": "RETRIES_EXHAUSTED", "message": final.error["message"],
                           "last_error_code": "X_TRANSIENT", "attempts": 5}


def test_max_attempts_one_dead_letters_on_first_failure():
    assert isinstance(plan_failure(job(0), T, settings(max_attempts=1), NOW), DeadLetterPlan)


def test_fatal_errors_are_rejected_by_the_planner():
    with pytest.raises(ValueError):
        plan_failure(job(), FatalStageError("X", "x"), settings(), NOW)


# ---------------------------------------------------------------- RateLimited / QuotaExhausted / infra
@pytest.mark.parametrize("retry_after,expected", [(None, 10), (3, 10), (45, 45), (120, 120), (3600, 3600)])
def test_rate_limited_uses_max_of_step_and_retry_after(retry_after, expected):
    plan = plan_failure(job(), RateLimited("R", "r", retry_after_sec=retry_after), settings(), NOW)
    assert plan.delay_sec == expected
    assert plan.next_attempt_at == NOW + timedelta(seconds=expected)        # the ACTUAL selected delay
    assert plan.retry_doc["next_attempt_at"] == (NOW + timedelta(seconds=expected)).isoformat()


def test_quota_uses_normal_backoff_not_the_reset_time_and_keeps_resets_at():
    quota = QuotaExhausted("GEMINI_QUOTA_EXHAUSTED", "q", resets_at=RESET)
    plan = plan_failure(job(), quota, settings(), NOW)
    assert plan.delay_sec == 10 and plan.retry_doc["resets_at"] == RESET.isoformat()
    final = plan_failure(job(4), quota, settings(), NOW)
    assert final.error["last_error_code"] == "GEMINI_QUOTA_EXHAUSTED"
    assert final.error["resets_at"] == RESET.isoformat()                   # clarification A


def test_infra_budget_is_separate():
    infra = RateLimiterUnavailable("RATE_LIMITER_UNAVAILABLE", "u")
    plan = plan_failure(job(attempts=4, infra=0), infra, settings(), NOW)    # provider budget nearly used: irrelevant
    assert isinstance(plan, RetryPlan) and plan.attempt_column == "infra_attempts" and plan.attempt == 1
    assert plan.delay_sec == 30 and plan.max_attempts == 20
    final = plan_failure(job(attempts=0, infra=19), infra, settings(), NOW)
    assert isinstance(final, DeadLetterPlan) and final.attempt_column == "infra_attempts"
    assert final.error["last_error_code"] == "RATE_LIMITER_UNAVAILABLE"


def test_provider_errors_use_attempts_column():
    assert plan_failure(job(), T, settings(), NOW).attempt_column == "attempts"


# ---------------------------------------------------------------- identity (clarification C)
def test_every_plan_gets_a_new_event_id_and_doc_carries_it():
    ids = {plan_failure(job(), T, settings(), NOW).event_id for _ in range(50)}
    assert len(ids) == 50
    plan = plan_failure(job(), T, settings(), NOW)
    assert plan.retry_doc["event_id"] == str(plan.event_id)
    dead = plan_failure(job(4), T, settings(), NOW)
    assert dead.event_id not in ids


def test_first_bucket_never_exceeds_delay_when_a_smaller_bucket_fits():
    plan = plan_failure(job(), RateLimited("R", "r", retry_after_sec=120), settings(), NOW)
    assert plan.routing_key_seconds == 90                                  # then hops for the remaining 30 s


def test_seconds_until():
    doc = {"next_attempt_at": (NOW + timedelta(seconds=7)).isoformat()}
    assert seconds_until(doc, NOW) == 7 and seconds_until(doc, NOW + timedelta(seconds=8)) == -1


# ---------------------------------------------------------------- API view hides internals
def test_public_retry_view():
    doc = plan_failure(job(), T, settings(), NOW).retry_doc
    view = public_retry_view(doc)
    assert view == {"reason": "X_TRANSIENT", "attempt": 1, "max_attempts": 5, "next_attempt_at": doc["next_attempt_at"]}
    assert "event_id" not in view and "budget" not in view
    assert public_retry_view(None) is None
