"""Pure HTTP/exception classification (ADR-10). No Docker, no network."""
import ast
import json
import socket
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import notesagent.common.http_errors as http_errors
from notesagent.common.errors import (
    FatalStageError,
    PipelineError,
    Provider,
    QuotaExhausted,
    RateLimited,
    RetryBudget,
    TransientError,
)
from notesagent.common.http_errors import (
    PACIFIC,
    classify_exception,
    classify_http,
    next_pacific_midnight,
    parse_retry_after,
)
from notesagent.common.logging import redact

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
G, N, Y, H = Provider.GEMINI, Provider.NOTION, Provider.YOUTUBE, Provider.GENERIC


def c(provider, status, headers=None, body=None, **kw):
    return classify_http(provider, status, headers, body, now=NOW, **kw)


def notion_body(code, **additional):
    return {"object": "error", "status": 0, "code": code, "message": "m", "additional_data": additional}


# ===================================================================== table 4.1 generic
@pytest.mark.parametrize("status,cls,code", [
    (200, None, None), (204, None, None),
    (408, TransientError, "HTTP_HTTP_408"), (425, TransientError, "HTTP_HTTP_425"),
    (500, TransientError, "HTTP_HTTP_500"), (502, TransientError, "HTTP_HTTP_502"),
    (503, TransientError, "HTTP_HTTP_503"), (504, TransientError, "HTTP_HTTP_504"),
    (429, RateLimited, "HTTP_RATE_LIMITED"),
    (400, FatalStageError, "HTTP_HTTP_400"), (418, FatalStageError, "HTTP_HTTP_418"),
    (301, FatalStageError, "HTTP_UNEXPECTED_HTTP_STATUS"), (101, FatalStageError, "HTTP_UNEXPECTED_HTTP_STATUS"),
])
def test_generic_table(status, cls, code):
    result = c(H, status)
    if cls is None:
        assert result is None
    else:
        assert type(result) is cls and result.code == code and result.detail["http_status"] == status


# ===================================================================== table 4.2 Notion
@pytest.mark.parametrize("status,code,cls,expected", [
    (401, "unauthorized", FatalStageError, "NOTION_UNAUTHORIZED"),
    (403, "restricted_resource", FatalStageError, "NOTION_FORBIDDEN"),
    (404, "object_not_found", FatalStageError, "NOTION_OBJECT_NOT_FOUND"),
    (400, "validation_error", FatalStageError, "NOTION_BAD_REQUEST"),
    (400, "invalid_json", FatalStageError, "NOTION_BAD_REQUEST"),
    (400, "invalid_request", FatalStageError, "NOTION_BAD_REQUEST"),
    (400, "invalid_request_url", FatalStageError, "NOTION_BAD_REQUEST"),
    (400, "missing_version", FatalStageError, "NOTION_BAD_REQUEST"),
    (400, "invalid_grant", FatalStageError, "NOTION_BAD_REQUEST"),
    (400, "invalid_beta", FatalStageError, "NOTION_BAD_REQUEST"),
    (409, "conflict_error", TransientError, "NOTION_CONFLICT"),                       # D6
    (500, "internal_server_error", TransientError, "NOTION_HTTP_500"),
    (502, "bad_gateway", TransientError, "NOTION_HTTP_502"),
    (503, "service_unavailable", TransientError, "NOTION_HTTP_503"),
    (503, "database_connection_unavailable", TransientError, "NOTION_HTTP_503"),
    (504, "gateway_timeout", TransientError, "NOTION_HTTP_504"),
    (429, "rate_limited", RateLimited, "NOTION_RATE_LIMITED"),
    (529, "service_overload", RateLimited, "NOTION_SERVICE_OVERLOAD"),
])
def test_notion_table(status, code, cls, expected):
    result = c(N, status, body=notion_body(code))
    assert type(result) is cls and result.code == expected and result.detail["provider_code"] == code


def test_notion_retry_after_header_integer_seconds():
    assert c(N, 429, {"Retry-After": "7"}, notion_body("rate_limited")).retry_after_sec == 7


def test_notion_workspace_limit_retry_after_above_a_minute_is_kept():
    assert c(N, 429, {"retry-after": "120"}, notion_body("rate_limited")).retry_after_sec == 120


def test_notion_retry_after_from_body_when_header_missing():
    assert c(N, 429, None, notion_body("rate_limited", retry_after="11")).retry_after_sec == 11


def test_notion_header_beats_body():
    assert c(N, 429, {"Retry-After": "3"}, notion_body("rate_limited", retry_after="50")).retry_after_sec == 3


def test_notion_request_blocked_is_fatal():
    result = c(N, 429, {"Retry-After": "5"}, notion_body("rate_limited", rate_limit_reason="public_api_request_blocked"))
    assert type(result) is FatalStageError and result.code == "NOTION_REQUEST_BLOCKED"


def test_notion_service_overload_honours_retry_after():
    assert c(N, 529, {"Retry-After": "9"}, notion_body("service_overload")).retry_after_sec == 9


def test_notion_write_with_5xx_flags_unknown_outcome():
    body = notion_body("service_unavailable", retry_guidance=["Read the object again.", "Do not repeat the write."],
                       committed_resource_id="0123456789abcdef0123456789abcdef")
    result = c(N, 503, body=body, method="PATCH")
    assert type(result) is TransientError
    assert result.detail["notion_write_outcome_unknown"] is True
    assert result.detail["committed_resource_id"] == "0123456789abcdef0123456789abcdef"
    assert "Do not repeat" not in repr(result.detail)                                # guidance text not copied


def test_notion_idempotent_read_5xx_is_plain_transient():
    result = c(N, 502, body=notion_body("bad_gateway"), method="GET")
    assert result.detail["notion_write_outcome_unknown"] is False and "committed_resource_id" not in result.detail


def test_notion_committed_id_must_look_like_a_uuid():
    result = c(N, 503, body=notion_body("service_unavailable", committed_resource_id="<script>"), method="POST")
    assert "committed_resource_id" not in result.detail


# ---------------------------------------------------------------- write-outcome flag: conservative allow-list
METHOD_TABLE = [
    ("GET", False), ("HEAD", False), ("OPTIONS", False), ("DELETE", False),
    ("get", False), ("head", False), ("options", False), ("delete", False), (" Get ", False),
    ("POST", True), ("PATCH", True), ("PUT", True), ("post", True), ("patch", True), ("put", True),
    (None, True),                     # caller forgot the method -> assume the write outcome is unknown
    ("BREW", True), ("", True), ("GETX", True), (123, True),   # unknown / malformed -> unknown
]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
@pytest.mark.parametrize("method,unknown", METHOD_TABLE)
def test_notion_write_outcome_flag_allow_list(status, method, unknown):
    result = c(N, status, body=notion_body("x"), method=method)
    assert type(result) is TransientError
    assert result.detail["notion_write_outcome_unknown"] is unknown


def test_notion_flag_default_when_method_omitted_entirely():
    result = classify_http(N, 503, None, notion_body("service_unavailable"), now=NOW)
    assert result.detail["notion_write_outcome_unknown"] is True


@pytest.mark.parametrize("safe_method", ["GET", "HEAD", "OPTIONS", "DELETE"])
def test_retry_guidance_overrides_a_safe_method(safe_method):
    body = notion_body("service_unavailable", retry_guidance=["Do not repeat the write."])
    assert c(N, 503, body=body, method=safe_method).detail["notion_write_outcome_unknown"] is True


@pytest.mark.parametrize("guidance,expected", [([], False), (None, False), ("", False), (["x"], True)])
def test_only_non_empty_retry_guidance_overrides(guidance, expected):
    body = notion_body("service_unavailable", retry_guidance=guidance)
    assert c(N, 503, body=body, method="GET").detail["notion_write_outcome_unknown"] is expected


@pytest.mark.parametrize("candidate,kept", [
    ("0123456789abcdef0123456789abcdef", True),
    ("01234567-89ab-cdef-0123-456789abcdef", True),
    ("01234567-89AB-CDEF-0123-456789ABCDEF", True),
    ("0123456789abcdef0123456789abcde", False),        # 31 hex
    ("0123456789abcdef0123456789abcdefa", False),      # 33 hex
    ("0123456789abcdef0123456789abcdeg", False),       # non-hex
    ("../../etc/passwd", False), ("<script>alert(1)</script>", False),
    ("0123456789abcdef0123456789abcdef\nInjected", False),
    (12345, False), (None, False), (["0123456789abcdef0123456789abcdef"], False),
])
def test_committed_resource_id_validation(candidate, kept):
    body = notion_body("service_unavailable", committed_resource_id=candidate)
    detail = c(N, 503, body=body, method="POST").detail
    assert ("committed_resource_id" in detail) is kept
    if kept:
        assert detail["committed_resource_id"] == candidate


def test_no_retry_guidance_or_upstream_text_leaks():
    marker = "UPSTREAM-SECRET-TEXT ntn_abc123 AIzaSyA1234567890abcdefghijklmnopqrstuv"
    body = {"object": "error", "code": "service_unavailable", "message": marker,
            "additional_data": {"retry_guidance": [marker, "Do not repeat the write."],
                                "committed_resource_id": "0123456789abcdef0123456789abcdef",
                                "from": marker, "elapsed_ms": 61000}}
    for method in ("POST", "GET", None):
        result = c(N, 503, body=body, method=method)
        rendered = f"{result} {result!r} {result.message} {result.detail}"
        assert "UPSTREAM-SECRET-TEXT" not in rendered and "Do not repeat" not in rendered
        assert set(result.detail) <= {"http_status", "provider_code", "notion_write_outcome_unknown",
                                      "committed_resource_id"}


def test_flag_only_on_notion_5xx():
    for status in (400, 401, 404, 409, 429, 529):
        assert "notion_write_outcome_unknown" not in c(N, status, body=notion_body("x"), method="POST").detail
    assert "notion_write_outcome_unknown" not in c(G, 503, method="POST").detail


# ===================================================================== table 4.3 Gemini
def gemini_v1(status_code, status_text, details=None):
    return {"error": {"code": status_code, "message": "m", "status": status_text, "details": details or []}}


def gemini_v2(code):
    return {"error": {"code": code, "message": "m"}}


@pytest.mark.parametrize("status,cls,code", [
    (400, FatalStageError, "GEMINI_BAD_REQUEST"), (401, FatalStageError, "GEMINI_AUTH"),
    (403, FatalStageError, "GEMINI_AUTH"), (402, FatalStageError, "GEMINI_PAYMENT_REQUIRED"),
    (404, FatalStageError, "GEMINI_MODEL_NOT_FOUND"),
    (500, TransientError, "GEMINI_HTTP_500"), (503, TransientError, "GEMINI_HTTP_503"),
    (504, TransientError, "GEMINI_HTTP_504"), (502, TransientError, "GEMINI_HTTP_502"),
])
def test_gemini_table(status, cls, code):
    result = c(G, status, body=gemini_v1(status, "X"))
    assert type(result) is cls and result.code == code


@pytest.mark.parametrize("body", [gemini_v2("rate_limit_exceeded"), gemini_v1(429, "RESOURCE_EXHAUSTED"), None, b""])
def test_gemini_rate_limited(body):
    result = c(G, 429, body=body)
    assert type(result) is RateLimited and result.code == "GEMINI_RATE_LIMITED"


@pytest.mark.parametrize("body", [
    gemini_v2("quota_exceeded"),
    gemini_v1(429, "RESOURCE_EXHAUSTED", [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                          "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]),
])
def test_gemini_daily_quota(body):
    result = c(G, 429, body=body)
    assert type(result) is QuotaExhausted and result.code == "GEMINI_QUOTA_EXHAUSTED"
    assert result.resets_at == datetime(2026, 11, 20, 0, 0, tzinfo=PACIFIC)


def test_gemini_per_minute_quota_failure_is_rate_limit_not_daily():
    body = gemini_v1(429, "RESOURCE_EXHAUSTED", [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                                  "violations": [{"quotaId": "GenerateRequestsPerMinutePerProject"}]}])
    assert type(c(G, 429, body=body)) is RateLimited


@pytest.mark.parametrize("delay,expected", [("30s", 30), ("1.5s", 1.5), ("bad", None), ("99999s", 3600)])
def test_gemini_retry_info_delay(delay, expected):
    body = gemini_v1(429, "RESOURCE_EXHAUSTED", [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                  "retryDelay": delay}])
    assert c(G, 429, body=body).retry_after_sec == expected


def test_gemini_header_beats_retry_info():
    body = gemini_v1(429, "RESOURCE_EXHAUSTED", [{"@type": "x.RetryInfo", "retryDelay": "30s"}])
    assert c(G, 429, {"Retry-After": "4"}, body).retry_after_sec == 4


# ===================================================================== table 4.4 YouTube (D2, D3)
@pytest.mark.parametrize("status,cls,code", [
    (404, FatalStageError, "YOUTUBE_VIDEO_UNAVAILABLE"),     # D3: FAILED, not retried
    (410, FatalStageError, "YOUTUBE_VIDEO_UNAVAILABLE"),     # D3
    (403, TransientError, "YOUTUBE_FORBIDDEN"),              # D2
    (429, RateLimited, "YOUTUBE_RATE_LIMITED"),
    (500, TransientError, "YOUTUBE_HTTP_500"), (503, TransientError, "YOUTUBE_HTTP_503"),
    (401, FatalStageError, "YOUTUBE_HTTP_401"),
])
def test_youtube_table(status, cls, code):
    result = c(Y, status)
    assert type(result) is cls and result.code == code


def test_youtube_unavailable_uses_no_retry_budget():
    assert c(Y, 404).budget is RetryBudget.NONE and c(Y, 410).budget is RetryBudget.NONE


# ===================================================================== every status, every provider
@pytest.mark.parametrize("provider", list(Provider))
def test_every_status_is_classified_deterministically(provider):
    for status in range(100, 600):
        first = c(provider, status, {"Retry-After": "5"}, b"{}")
        second = c(provider, status, {"Retry-After": "5"}, b"{}")
        assert (first is None) == (200 <= status < 300)
        if first is not None:
            assert isinstance(first, PipelineError) and (type(first), first.code) == (type(second), second.code)


# ===================================================================== Retry-After parsing (requirement A)
@pytest.mark.parametrize("value,expected", [
    ("5", 5.0), ("0", 0.0), ("1.5", 1.5), (" 7 ", 7.0), (12, 12.0),
    ("-3", None), ("abc", None), ("inf", None), ("nan", None), ("", None), (None, None), (True, None),
    ("999999", 3600.0),
    ("Thu, 19 Nov 2026 12:00:30 GMT", 30.0),                 # future HTTP date
    ("Thu, 19 Nov 2026 11:59:00 GMT", None),                 # past HTTP date
    ("Fri, 20 Nov 2026 12:00:00 GMT", 3600.0),               # far future date -> capped
])
def test_parse_retry_after(value, expected):
    assert parse_retry_after(value, now=NOW) == expected


def test_retry_after_cap_is_configurable():
    assert parse_retry_after("5000", now=NOW, cap=600) == 600


@pytest.mark.parametrize("headers", [{"RETRY-AFTER": "8"}, {"retry-after": "8"}, [("Retry-After", "8")]])
def test_header_name_case_insensitive(headers):
    assert c(H, 429, headers).retry_after_sec == 8


# ===================================================================== quota reset is absolute, never capped
@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 11, 19, 8, 30, tzinfo=timezone.utc), datetime(2026, 11, 20, 0, 0, tzinfo=PACIFIC)),   # 00:30 PST
    (datetime(2026, 11, 20, 7, 59, 59, tzinfo=timezone.utc), datetime(2026, 11, 20, 0, 0, tzinfo=PACIFIC)),  # 23:59:59
    (datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc), datetime(2026, 3, 8, 0, 0, tzinfo=PACIFIC)),   # before spring DST
    (datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc), datetime(2026, 3, 9, 0, 0, tzinfo=PACIFIC)),   # DST day
    (datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc), datetime(2026, 11, 1, 0, 0, tzinfo=PACIFIC)),
    (datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc), datetime(2026, 11, 2, 0, 0, tzinfo=PACIFIC)),  # fall-back day
])
def test_next_pacific_midnight(now, expected):
    result = next_pacific_midnight(now)
    assert result == expected and result.tzinfo is not None and result > now


def test_quota_reset_is_not_capped_to_one_hour():
    now = datetime(2026, 11, 19, 8, 30, tzinfo=timezone.utc)                       # 00:30 Pacific
    result = classify_http(G, 429, None, gemini_v2("quota_exceeded"), now=now)
    remaining = result.resets_at - now
    assert timedelta(hours=23, minutes=29) < remaining <= timedelta(hours=23, minutes=30)


def test_fall_back_day_reset_can_be_25_hours_away():
    now = datetime(2026, 11, 1, 7, 0, 1, tzinfo=timezone.utc)                      # 00:00:01 PDT on 1 Nov
    assert next_pacific_midnight(now) - now > timedelta(hours=24, minutes=59)


# ===================================================================== malformed bodies
@pytest.mark.parametrize("body", [b"\xff\xfe\x00garbage", b"[1,2,3]", b"null", "not json", "x" * 1_000_000,
                                  b"{" * 100_000, 12345, ["list"], {"error": "string"}])
def test_malformed_bodies_never_raise_and_fall_back_to_status(body):
    for provider in Provider:
        for status in (400, 429, 500, 503):
            result = c(provider, status, None, body)
            assert isinstance(result, PipelineError)


# ===================================================================== exceptions / TLS ordering (B, D4, D4a)
def wrap(inner, depth):
    exc = inner
    for _ in range(depth):
        try:
            try:
                raise exc
            except BaseException as caught:
                raise ConnectionError("wrapped") from caught
        except ConnectionError as outer:
            exc = outer
    return exc


@pytest.mark.parametrize("exc,cls,code", [
    (ssl.SSLCertVerificationError("bad cert"), FatalStageError, "GEMINI_TLS_ERROR"),
    (ssl.CertificateError("hostname mismatch"), FatalStageError, "GEMINI_TLS_ERROR"),
    (ssl.SSLError("handshake failure"), FatalStageError, "GEMINI_TLS_ERROR"),
    (ssl.SSLEOFError("eof"), TransientError, "GEMINI_TLS_CONNECTION_DROPPED"),          # D4a
    (ssl.SSLZeroReturnError("closed"), TransientError, "GEMINI_TLS_CONNECTION_DROPPED"),  # D4a
    (TimeoutError(), TransientError, "GEMINI_UNREACHABLE"),
    (ConnectionResetError(), TransientError, "GEMINI_UNREACHABLE"),
    (ConnectionRefusedError(), TransientError, "GEMINI_UNREACHABLE"),
    (socket.gaierror("dns"), TransientError, "GEMINI_UNREACHABLE"),
    (OSError("io"), TransientError, "GEMINI_UNREACHABLE"),
    (ValueError("bug"), TransientError, "UNEXPECTED_ERROR"),                              # D1
    (KeyError("bug"), TransientError, "UNEXPECTED_ERROR"),
])
def test_exception_classification(exc, cls, code):
    result = classify_exception(G, exc)
    assert type(result) is cls and result.code == code and result.detail["exception_type"] == type(exc).__name__


def test_cert_error_is_an_oserror_but_still_fatal():
    assert issubclass(ssl.SSLCertVerificationError, OSError)
    assert type(classify_exception(N, ssl.SSLCertVerificationError())) is FatalStageError


@pytest.mark.parametrize("depth", [1, 2, 5])
def test_wrapped_tls_errors_are_found(depth):
    assert classify_exception(N, wrap(ssl.SSLCertVerificationError("x"), depth)).code == "NOTION_TLS_ERROR"
    assert classify_exception(N, wrap(ssl.SSLEOFError("x"), depth)).code == "NOTION_TLS_CONNECTION_DROPPED"


def test_tls_beyond_agreed_depth_is_not_followed():
    assert classify_exception(N, wrap(ssl.SSLCertVerificationError("x"), 6)).code == "NOTION_UNREACHABLE"


def test_cyclic_exception_chain_terminates():
    a, b = ValueError("a"), ValueError("b")
    a.__context__, b.__context__ = b, a
    assert classify_exception(G, a).code == "UNEXPECTED_ERROR"


def test_classified_errors_pass_through():
    err = FatalStageError("X", "x")
    assert classify_exception(G, err) is err


# ===================================================================== no secrets anywhere (C)
SECRET_KEY = "AIzaSyA1234567890abcdefghijklmnopqrstuv"


def test_no_upstream_text_in_results():
    body = json.dumps({"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                 "message": f"API key {SECRET_KEY} not valid; token ntn_abc123; Bearer xyz"}})
    exc = ConnectionError(f"GET https://x/v1?key={SECRET_KEY} failed with ntn_abc123")
    for result in (c(G, 400, None, body), c(N, 401, None, body), classify_exception(G, exc)):
        rendered = f"{result} {result!r} {result.message} {result.detail}"
        assert SECRET_KEY not in rendered and "ntn_abc123" not in rendered and "Bearer xyz" not in rendered


def test_non_whitelisted_provider_codes_are_dropped():
    assert c(N, 400, body={"code": "has spaces <script>"}).detail["provider_code"] is None


def test_logging_redacts_google_keys_and_key_params():
    text = f"calling https://generativelanguage.googleapis.com/v1beta/models?key={SECRET_KEY}&alt=json and {SECRET_KEY}"
    out = redact(text)
    assert SECRET_KEY not in out and "key=[REDACTED]" in out and "&alt=json" in out


# ===================================================================== purity: no infrastructure (requirement 5)
def test_classifier_module_imports_no_infrastructure():
    tree = ast.parse(Path(http_errors.__file__).read_text(encoding="utf-8"))
    imported = {(n.module if isinstance(n, ast.ImportFrom) else a.name).split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [n])}
    from_notesagent = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
                       and n.module.startswith("notesagent")}
    assert from_notesagent == {"notesagent.common.errors"}
    assert not imported & {"psycopg", "aio_pika", "aiormq", "redis", "httpx", "fastapi", "requests"}


def test_classification_performs_no_network_io(monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("classification attempted network I/O")
    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    for provider in Provider:
        for status in (200, 400, 401, 404, 409, 429, 500, 503, 529):
            c(provider, status, {"Retry-After": "Thu, 19 Nov 2026 12:00:30 GMT"}, b'{"code":"x"}', method="POST")
        classify_exception(provider, ssl.SSLEOFError())
        next_pacific_midnight(NOW)
