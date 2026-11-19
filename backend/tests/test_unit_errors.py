"""Error classes: budgets, backward compatibility, safety of str/repr/detail, raise_classified."""
import logging
import traceback
from datetime import datetime, timezone

import pytest

from notesagent.common.errors import (
    FatalStageError,
    PipelineError,
    Provider,
    QuotaExhausted,
    RateLimited,
    RateLimiterUnavailable,
    RetryableError,
    RetryBudget,
    TransientError,
    raise_classified,
)


# ---------------------------------------------------------------- backward compatibility (requirement D)
def test_fatal_stage_error_phase1_constructor_unchanged():
    err = FatalStageError("PIPELINE_STAGE_UNAVAILABLE", "The stage is not available.")
    assert err.code == "PIPELINE_STAGE_UNAVAILABLE" and err.message == "The stage is not available."
    assert isinstance(err, Exception) and isinstance(err, PipelineError)
    assert err.budget is RetryBudget.NONE and not err.retryable
    with pytest.raises(FatalStageError) as info:                     # existing `except FatalStageError` still works
        raise err
    assert info.value.code == "PIPELINE_STAGE_UNAVAILABLE"


def test_fatal_stage_error_positional_signature():
    import inspect
    params = list(inspect.signature(FatalStageError.__init__).parameters)
    assert params[:3] == ["self", "code", "message"]


# ---------------------------------------------------------------- budgets
@pytest.mark.parametrize("err,budget", [
    (FatalStageError("X", "x"), RetryBudget.NONE),
    (TransientError("X", "x"), RetryBudget.PROVIDER),
    (RateLimited("X", "x", retry_after_sec=5), RetryBudget.PROVIDER),
    (QuotaExhausted("X", "x", resets_at=datetime(2026, 11, 20, tzinfo=timezone.utc)), RetryBudget.PROVIDER),
    (RateLimiterUnavailable("X", "x"), RetryBudget.INFRASTRUCTURE),
])
def test_budgets(err, budget):
    assert err.budget is budget
    assert err.retryable == (budget is not RetryBudget.NONE)
    assert isinstance(err, RetryableError) == (budget is not RetryBudget.NONE)


def test_rate_limiter_unavailable_never_uses_provider_budget():
    assert RateLimiterUnavailable("RATE_LIMITER_UNAVAILABLE", "x").budget is not RetryBudget.PROVIDER


def test_quota_reset_must_be_timezone_aware_and_is_not_a_relative_delay():
    with pytest.raises(ValueError):
        QuotaExhausted("X", "x", resets_at=datetime(2026, 11, 20))
    err = QuotaExhausted("X", "x", resets_at=datetime(2026, 11, 21, 8, tzinfo=timezone.utc))
    assert err.resets_at.tzinfo is not None and "retry_after_sec" not in err.detail


# ---------------------------------------------------------------- safety (requirement C)
SECRETS = ["AIzaSyA1234567890abcdefghijklmnopqrstuv", "ntn_s3cr3tT0k3nValue", "Bearer abc.def.ghi"]


def test_detail_is_whitelisted_and_scalar_only():
    err = TransientError("X", "x", detail={"http_status": 503, "body": SECRETS[0], "url": "https://h/?key=" + SECRETS[0],
                                           "provider_code": "x" * 500, "nested": {"a": 1}})
    assert set(err.detail) == {"http_status", "provider_code"} and len(err.detail["provider_code"]) == 64
    assert SECRETS[0] not in repr(err.detail)


def test_str_and_repr_show_only_code_message_provider():
    err = RateLimited("GEMINI_RATE_LIMITED", "Gemini is rate limiting requests.", provider=Provider.GEMINI,
                      retry_after_sec=3)
    assert repr(err) == "RateLimited(code='GEMINI_RATE_LIMITED', provider='GEMINI')"
    assert str(err) == "GEMINI_RATE_LIMITED: Gemini is rate limiting requests."


def test_message_is_bounded():
    assert len(FatalStageError("X", "m" * 5000).message) == 200


def test_raise_classified_hides_upstream_exception_from_tracebacks_and_logs(caplog):
    upstream = ConnectionError(f"failed GET https://generativelanguage.googleapis.com/v1?key={SECRETS[0]} "
                               f"Authorization: {SECRETS[2]} token {SECRETS[1]}")
    with pytest.raises(TransientError) as info:
        try:
            raise upstream
        except ConnectionError:
            raise_classified(TransientError("GEMINI_UNREACHABLE", "The provider could not be reached.",
                                            provider=Provider.GEMINI))
    err = info.value
    assert err.__cause__ is None and err.__suppress_context__ is True
    rendered = "".join(traceback.format_exception(err))
    with caplog.at_level(logging.ERROR):
        logging.getLogger("t").error("failed", exc_info=err)
    for secret in SECRETS:
        assert secret not in rendered and secret not in caplog.text
        assert secret not in str(err) and secret not in repr(err)
