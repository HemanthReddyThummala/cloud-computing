"""ADR-21 unit tests: Gemini 503/504 classification (sanitized status, Retry-After), the Gemini-specific
backoff, terminal error documents, the request-timeout setting, and the wiring of that timeout into the
REAL google-genai request (mock httpx2 transport — no network)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx2
import pytest

from notesagent.common.config import Settings, get_settings
from notesagent.common.errors import Provider, RateLimited, TransientError
from notesagent.common.http_errors import classify_http
from notesagent.worker.gemini import GeminiClient, classify_gemini_error
from notesagent.worker.retry_policy import DeadLetterPlan, RetryPlan, plan_failure, public_retry_view

G = Provider.GEMINI
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
UNAVAILABLE = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "The model is overloaded. TOPSECRET"}}
DEADLINE = {"error": {"code": 504, "status": "DEADLINE_EXCEEDED", "message": "Deadline expired. TOPSECRET"}}


def settings(**kw):
    base = dict(database_url="postgresql://u:p@h/d", rabbitmq_url="amqp://u:p@h/", backoff_schedule_sec="10,30,90",
                gemini_unavailable_backoff_sec="60,180,600", max_attempts=5)
    return Settings(**{**base, **kw})


def job(attempts=0):
    return {"attempts": attempts, "infra_attempts": 0}


# ===================================================================== classification
@pytest.mark.parametrize("status,body,google", [(503, UNAVAILABLE, "UNAVAILABLE"), (504, DEADLINE, "DEADLINE_EXCEEDED")])
def test_503_504_keep_http_status_and_sanitized_google_status_only(status, body, google):
    err = classify_http(G, status, None, body, now=NOW)
    assert isinstance(err, TransientError) and err.code == f"GEMINI_HTTP_{status}"
    assert err.detail == {"http_status": status, "provider_code": google, "retry_after_sec": None}
    assert "TOPSECRET" not in repr(err) + str(err) + json.dumps(err.detail)


def test_503_retry_after_header_wins_then_retry_info_then_none():
    assert classify_http(G, 503, {"Retry-After": "42"}, UNAVAILABLE, now=NOW).detail["retry_after_sec"] == 42
    with_info = {"error": {**UNAVAILABLE["error"], "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}}
    assert classify_http(G, 503, None, with_info, now=NOW).detail["retry_after_sec"] == 17
    assert classify_http(G, 503, {"Retry-After": "5"}, with_info, now=NOW).detail["retry_after_sec"] == 5
    for bad in ("-1", "soon", "", "nan"):
        assert classify_http(G, 503, {"Retry-After": bad}, UNAVAILABLE, now=NOW).detail["retry_after_sec"] is None
    capped = classify_http(G, 504, {"Retry-After": "999999"}, DEADLINE, now=NOW, retry_after_cap=3600)
    assert capped.detail["retry_after_sec"] == 3600


def test_500_and_unsafe_provider_codes():
    assert "retry_after_sec" not in classify_http(G, 500, {"Retry-After": "9"}, None, now=NOW).detail
    evil = {"error": {"code": 503, "status": "UNAVAILABLE <script>alert(1)</script>"}}
    detail = classify_http(G, 503, None, evil, now=NOW).detail
    assert detail["provider_code"] == "503"            # unsafe status rejected -> Google's numeric error.code
    assert "<script>" not in json.dumps(detail)
    worse = {"error": {"code": "5 0 3", "status": "<script>"}}
    assert classify_http(G, 503, None, worse, now=NOW).detail["provider_code"] is None


def test_real_sdk_exception_is_classified_with_status_and_retry_after():
    from google.genai import errors
    exc = errors.ServerError(503, UNAVAILABLE, httpx2.Response(503, headers={"Retry-After": "11"}, json=UNAVAILABLE))
    err = classify_gemini_error(exc)
    assert err.code == "GEMINI_HTTP_503"
    assert err.detail == {"http_status": 503, "provider_code": "UNAVAILABLE", "retry_after_sec": 11}


# ===================================================================== retry policy
def gemini(status=503, retry_after=None):
    code = "UNAVAILABLE" if status == 503 else "DEADLINE_EXCEEDED"
    return TransientError(f"GEMINI_HTTP_{status}", "u", provider=G,
                          detail={"http_status": status, "provider_code": code, "retry_after_sec": retry_after})


@pytest.mark.parametrize("status", [503, 504])
def test_gemini_unavailable_uses_its_own_schedule_bounded_by_max_attempts(status):
    s = settings()
    delays = []
    for n in range(4):
        plan = plan_failure(job(n), gemini(status), s, NOW)
        assert isinstance(plan, RetryPlan)
        assert plan.retry_doc["backoff"] == "gemini_unavailable"
        assert (plan.retry_doc["http_status"], plan.retry_doc["provider_code"]) == (
            status, "UNAVAILABLE" if status == 503 else "DEADLINE_EXCEEDED")
        delays.append(plan.delay_sec)
    assert delays == [60, 180, 600, 600]
    final = plan_failure(job(4), gemini(status), s, NOW)
    assert isinstance(final, DeadLetterPlan) and final.attempt == 5
    assert final.error["last_error_code"] == f"GEMINI_HTTP_{status}"
    assert final.error["last_http_status"] == status and final.error["attempts"] == 5


@pytest.mark.parametrize("retry_after,expected", [(None, 60), (30, 60), (61.5, 61.5), (900, 900), (3600, 3600),
                                                  (0, 60), (-5, 60), (float("nan"), 60), (True, 60), ("120", 60)])
def test_gemini_unavailable_honours_only_valid_retry_after(retry_after, expected):
    err = gemini(503)
    err.detail["retry_after_sec"] = retry_after          # also exercises values the classifier never produces
    plan = plan_failure(job(), err, settings(), NOW)
    assert plan.delay_sec == expected
    assert plan.next_attempt_at == NOW + timedelta(seconds=expected)
    assert plan.routing_key_seconds in settings().delay_queue_seconds          # existing queues only (hops)


def test_other_errors_keep_the_default_schedule():
    s = settings()
    others = [TransientError("GEMINI_HTTP_500", "x", provider=G, detail={"http_status": 500}),
              TransientError("GEMINI_UNREACHABLE", "x", provider=G),
              TransientError("YOUTUBE_HTTP_503", "x", provider=Provider.YOUTUBE, detail={"http_status": 503}),
              TransientError("NOTION_HTTP_503", "x", provider=Provider.NOTION, detail={"http_status": 503}),
              TransientError("NOTION_HTTP_504", "x", provider=Provider.NOTION,
                             detail={"http_status": 504, "retry_after_sec": 500}),
              RateLimited("GEMINI_RATE_LIMITED", "x", provider=G, retry_after_sec=None)]
    for err in others:
        plan = plan_failure(job(), err, s, NOW)
        assert (plan.delay_sec, plan.retry_doc["backoff"]) == (10, "default"), err.code


def test_status_fields_are_omitted_when_unknown_and_public_view_is_unchanged():
    plan = plan_failure(job(), TransientError("X", "x"), settings(), NOW)
    assert "http_status" not in plan.retry_doc and "provider_code" not in plan.retry_doc
    view = public_retry_view(plan_failure(job(), gemini(503), settings(), NOW).retry_doc)
    assert set(view) == {"reason", "attempt", "max_attempts", "next_attempt_at"}
    final = plan_failure(job(4), TransientError("X", "x"), settings(), NOW)
    assert "last_http_status" not in final.error and "last_provider_code" not in final.error


def test_delay_queue_topology_is_not_changed_by_the_gemini_schedule():
    s = settings(gemini_unavailable_backoff_sec="60,180,600")
    assert s.delay_queue_seconds == sorted({10, 30, 90, s.lock_retry_delay_sec, s.infra_retry_delay_sec})


# ===================================================================== settings
def test_timeout_and_schedule_defaults_and_validation():
    s = get_settings()
    assert Settings.model_fields["gemini_request_timeout_sec"].default == 300.0
    assert Settings.model_fields["gemini_unavailable_backoff_sec"].default == "60,180,600"
    assert get_settings(gemini_request_timeout_sec=45).gemini_request_timeout_sec == 45
    assert get_settings(gemini_unavailable_backoff_sec=" 5, 7 ").gemini_unavailable_backoff_delays == [5, 7]
    for bad in ({"gemini_request_timeout_sec": 0}, {"gemini_request_timeout_sec": 601},
                {"gemini_unavailable_backoff_sec": "60,abc"}, {"gemini_unavailable_backoff_sec": "0"}):
        with pytest.raises(SystemExit):
            get_settings(**bad)
    assert s.gemini_unavailable_backoff_delays                     # conftest value parsed


# ===================================================================== timeout reaches the real SDK request
def run_client(monkeypatch, handler, timeout):
    """REAL GeminiClient + REAL google-genai request path; only the HTTP transport is a mock."""
    from google import genai
    real_client = genai.Client

    def client_with_mock_transport(*, api_key, http_options):
        opts = http_options.model_copy(update={"async_client_args": {"transport": httpx2.MockTransport(handler)}})
        return real_client(api_key=api_key, http_options=opts)

    monkeypatch.setattr(genai, "Client", client_with_mock_transport)
    s = get_settings(gemini_api_key="unit-test-key-not-real", gemini_request_timeout_sec=timeout)
    return asyncio.run(GeminiClient(s).generate(system="sys", prompt="hello", schema={"type": "object"}))


OK_BODY = {"candidates": [{"content": {"role": "model", "parts": [{"text": "{\"ok\": true}"}]}, "finishReason": "STOP"}],
           "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3}}


@pytest.mark.parametrize("timeout,header", [(300, "300"), (45.2, "46")])
def test_configured_timeout_reaches_the_sdk_request_and_server_deadline(monkeypatch, timeout, header):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(200, json=OK_BODY)

    reply = run_client(monkeypatch, handler, timeout)
    [request] = seen
    assert reply.text == '{"ok": true}' and reply.prompt_tokens == 12
    assert request.headers["X-Server-Timeout"] == header                  # Google's server deadline
    assert request.extensions["timeout"]["read"] == pytest.approx(timeout)  # client-side timeout
    assert request.url.path.endswith("/models/gemini-3.8-flash:generateContent")
    assert len(seen) == 1                                                 # SDK retries stay disabled


def test_503_through_the_real_sdk_is_one_request_and_keeps_only_the_status(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(503, headers={"Retry-After": "13"}, json=UNAVAILABLE)

    with pytest.raises(Exception) as caught:
        run_client(monkeypatch, handler, 300)
    err = classify_gemini_error(caught.value)
    assert len(calls) == 1
    assert err.code == "GEMINI_HTTP_503"
    assert err.detail == {"http_status": 503, "provider_code": "UNAVAILABLE", "retry_after_sec": 13}
    assert "TOPSECRET" not in repr(err) + str(err) + json.dumps(err.detail)
