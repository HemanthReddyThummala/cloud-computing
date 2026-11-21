"""Contract tests for the Gemini integration through the REAL google-genai 3.6.1 SDK request/response
path (only the HTTP transport is a mock; no network). Written for the 2026-11-21 503 investigation:

* pins the request we actually send (endpoint, version, fields, headers) so an SDK upgrade that changes
  it fails loudly;
* drives realistic Google error responses (503, 504, 400, 401, 403, 429 rate, 429 quota) through the SDK
  and our classifier, checking class, retry budget, sanitized detail and that nothing secret leaks;
* runs a structured-JSON success through the production SUMMARIZING stage (parse + validation).
"""
import asyncio
import json
import uuid

import httpx2
import pytest

from notesagent.common.config import get_settings
from notesagent.common.errors import (FatalStageError, QuotaExhausted, RateLimited, RetryBudget,
                                      TransientError)
from notesagent.worker.gemini import GeminiClient, classify_gemini_error
from notesagent.worker.stages import StageContext
from notesagent.worker.summarize import CHUNK_SCHEMA, SYSTEM, make_summarize_stage

FAKE_KEY = "AIzaContractTestKey-not-real-0123456789"


def sdk_client(monkeypatch, handler, **settings):
    """REAL GeminiClient + REAL SDK; the HTTP transport is a mock."""
    from google import genai
    real = genai.Client
    monkeypatch.setattr(genai, "Client", lambda *, api_key, http_options: real(
        api_key=api_key,
        http_options=http_options.model_copy(update={"async_client_args": {"transport": httpx2.MockTransport(handler)}})))
    return GeminiClient(get_settings(gemini_api_key=FAKE_KEY, **settings))


def ok(text, usage=None):
    return httpx2.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": usage or {"promptTokenCount": 321, "candidatesTokenCount": 45, "thoughtsTokenCount": 7}})


# ===================================================================== request shape (as investigated)
def test_request_shape_is_pinned(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return ok("{}")

    client = sdk_client(monkeypatch, handler)
    asyncio.run(client.generate(system=SYSTEM, prompt="Transcript:\n[#0 00:00] hi", schema=CHUNK_SCHEMA))
    [r] = seen
    body = json.loads(r.content)
    assert r.method == "POST"
    assert str(r.url) == "https://generativelanguage.googleapis.com/v1/models/gemini-3.5-flash-lite:generateContent"
    assert r.headers["x-goog-api-key"] == FAKE_KEY                     # key only in this header ...
    assert FAKE_KEY not in str(r.url) and FAKE_KEY not in r.content.decode()   # ... never in URL or body
    assert r.headers["x-server-timeout"] == "300"
    assert r.headers["x-goog-api-client"].startswith("google-genai-sdk/3.6.1")
    assert sorted(body) == ["contents", "generationConfig", "systemInstruction"]
    gc = body["generationConfig"]
    assert gc["responseMimeType"] == "application/json" and gc["responseJsonSchema"] == CHUNK_SCHEMA
    assert gc["maxOutputTokens"] == 8192
    # SDK 3.6.1 serializes ThinkingConfig fields in snake_case on v1 AND v1beta (identical in the one
    # real success on 2026-11-20) - recorded here so a change is noticed, not "fixed" blindly.
    assert gc["thinkingConfig"] == {"thinking_level": "LOW"}
    assert body["systemInstruction"]["parts"][0]["text"] == SYSTEM
    assert "tools" not in body and "temperature" not in gc and "topP" not in gc and "topK" not in gc


# ===================================================================== error responses through the SDK
LEAK = f"API key not valid. Please pass a valid API key. key={FAKE_KEY}"
CASES = [
    # status, headers, Google error body, expected class, code, budget, provider_code, retry_after
    (503, {}, {"code": 503, "status": "UNAVAILABLE", "message": "The model is overloaded."},
     TransientError, "GEMINI_HTTP_503", RetryBudget.PROVIDER, "UNAVAILABLE", None),
    (503, {"Retry-After": "21"}, {"code": 503, "status": "UNAVAILABLE", "message": "x"},
     TransientError, "GEMINI_HTTP_503", RetryBudget.PROVIDER, "UNAVAILABLE", 21),
    (504, {}, {"code": 504, "status": "DEADLINE_EXCEEDED", "message": "Deadline expired."},
     TransientError, "GEMINI_HTTP_504", RetryBudget.PROVIDER, "DEADLINE_EXCEEDED", None),
    (400, {}, {"code": 400, "status": "INVALID_ARGUMENT", "message": LEAK},
     FatalStageError, "GEMINI_BAD_REQUEST", RetryBudget.NONE, "INVALID_ARGUMENT", None),
    (401, {}, {"code": 401, "status": "UNAUTHENTICATED", "message": LEAK},
     FatalStageError, "GEMINI_AUTH", RetryBudget.NONE, "UNAUTHENTICATED", None),
    (403, {}, {"code": 403, "status": "PERMISSION_DENIED", "message": LEAK},
     FatalStageError, "GEMINI_AUTH", RetryBudget.NONE, "PERMISSION_DENIED", None),
    (429, {}, {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "rate",
               "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}]},
     RateLimited, "GEMINI_RATE_LIMITED", RetryBudget.PROVIDER, "RESOURCE_EXHAUSTED", 12),
    (429, {}, {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
               "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]},
     QuotaExhausted, "GEMINI_QUOTA_EXHAUSTED", RetryBudget.PROVIDER, "RESOURCE_EXHAUSTED", None),
]


@pytest.mark.parametrize("status,headers,error,cls,code,budget,provider_code,retry_after", CASES,
                         ids=[f"{c[0]}-{c[4]}" + ("-retry-after" if c[1] else "") for c in CASES])
def test_google_errors_through_the_sdk_are_classified_and_sanitized(monkeypatch, status, headers, error, cls,
                                                                   code, budget, provider_code, retry_after):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(status, headers=headers, json={"error": error})

    client = sdk_client(monkeypatch, handler)
    with pytest.raises(Exception) as caught:
        asyncio.run(client.generate(system="s", prompt="p", schema={"type": "object"}))
    err = classify_gemini_error(caught.value)
    assert len(calls) == 1                                               # SDK retries are off
    assert type(err) is cls and err.code == code and err.budget is budget
    assert err.detail["http_status"] == status and err.detail["provider_code"] == provider_code
    if cls is RateLimited:
        assert err.retry_after_sec == retry_after
    elif retry_after is not None:
        assert err.detail["retry_after_sec"] == retry_after
    rendered = repr(err) + str(err) + json.dumps(err.detail) + err.message
    assert FAKE_KEY not in rendered and "overloaded" not in rendered and "Deadline expired" not in rendered


def test_non_json_5xx_body_is_still_a_transient_503(monkeypatch):
    client = sdk_client(monkeypatch, lambda r: httpx2.Response(503, text="<html>Service Unavailable</html>"))
    with pytest.raises(Exception) as caught:
        asyncio.run(client.generate(system="s", prompt="p", schema={"type": "object"}))
    err = classify_gemini_error(caught.value)
    assert isinstance(err, TransientError) and err.code == "GEMINI_HTTP_503"


def test_transport_failure_is_unreachable_not_503(monkeypatch):
    def handler(request):
        raise httpx2.ConnectError("connection refused")

    client = sdk_client(monkeypatch, handler)
    with pytest.raises(Exception) as caught:
        asyncio.run(client.generate(system="s", prompt="p", schema={"type": "object"}))
    assert classify_gemini_error(caught.value).code == "GEMINI_UNREACHABLE"


# ===================================================================== structured JSON success, real stage
class _Limiter:
    def __init__(self):
        self.acquired, self.settled = [], []

    async def acquire(self, provider, *, tokens=0, **_):
        self.acquired.append(tokens)
        return object()

    async def settle(self, permit, tokens):
        self.settled.append(tokens)


SEGMENTS = [{"start": 0.0, "duration": 4.0, "end": 4.0, "text": "Welcome, today we look at sockets."},
            {"start": 4.0, "duration": 5.0, "end": 9.0, "text": "As you can see on this diagram, a socket pairs an IP and a port."},
            {"start": 30.0, "duration": 5.0, "end": 35.0, "text": "Here is the code for a TCP server."}]


def run_stage(monkeypatch, text):
    seen = []

    def handler(request):
        seen.append(request)
        return ok(text)

    client = sdk_client(monkeypatch, handler)
    settings = get_settings(gemini_api_key=FAKE_KEY)
    limiter = _Limiter()
    ctx = StageContext(job_id=uuid.uuid4(), job={"video_title": "Sockets"},
                       checkpoints={"TRANSCRIBING": {"title": "Sockets", "transcript": {"segments": SEGMENTS}}},
                       worker_id="test", _credential_reader=None, limiter=limiter)
    return asyncio.run(make_summarize_stage(settings, client)(ctx)), seen, limiter


def test_structured_json_success_is_parsed_validated_and_grounded(monkeypatch):
    notes = {"summary": "Sockets explained.", "key_points": ["A socket = IP + port"],
             "sections": [{"title": "Intro", "start_segment": 0, "points": ["Topic"]},
                          {"title": "Server code", "start_segment": 2, "points": ["TCP server"]}],
             "visual_moments": [{"segment": 1, "caption": "The speaker shows a socket diagram.", "reason": "says 'this diagram'"},
                                {"segment": 99, "caption": "invented", "reason": "not in transcript"}]}
    out, seen, limiter = run_stage(monkeypatch, json.dumps(notes))
    cp = out.checkpoint
    assert len(seen) == 1 and cp["gemini_requests"] == 1 and cp["chunk_count"] == 1
    assert cp["summary"] == "Sockets explained." and cp["key_points"] == ["A socket = IP + port"]
    assert [s["start_sec"] for s in cp["sections"]] == [0.0, 30.0]
    assert [(t["timestamp_sec"], t["caption"]) for t in cp["important_timestamps"]] == [
        (4.0, "The speaker shows a socket diagram.")]                    # invented segment 99 dropped
    assert cp["validation"]["dropped_timestamps"] == 1
    assert cp["usage"] == {"prompt_tokens": 321, "output_tokens": 45}
    assert len(limiter.acquired) == 1 and limiter.settled == [321]       # permit before, settle after


@pytest.mark.parametrize("text,reason", [("{not json", "schema_mismatch"),
                                         (json.dumps({"summary": 5}), "schema_mismatch")])
def test_invalid_structured_output_is_a_bounded_retryable_error(monkeypatch, text, reason):
    with pytest.raises(TransientError) as caught:
        run_stage(monkeypatch, text)
    assert caught.value.code == "GEMINI_INVALID_OUTPUT" and caught.value.detail["provider_code"] == reason
