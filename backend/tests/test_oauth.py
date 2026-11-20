"""ADR-18 Notion OAuth in the REAL gateway (TestClient), with real Redis/RabbitMQ/PostgreSQL and the fake
Notion server (separate process) as the token endpoint. Never contacts the real Notion API."""
import hashlib
import os
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import redis
from fastapi.testclient import TestClient

from tests.support.helpers import drain_outbox, wait_until

PORT = 18766
NOTION = f"http://127.0.0.1:{PORT}"
CLIENT_ID = "test-client-id-0001"
CLIENT_SECRET = "test-client-secret-should-never-appear-9876"
REDIRECT = "https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion"
REDIS_URL = os.environ["TEST_REDIS_URL"]


def _alive():
    try:
        return httpx.get(f"{NOTION}/_oauth", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture
def notion_server(procs, fx):
    procs.start("notion", "tests.support.fake_notion", PORT, FAKE_NOTION_CLIENT_ID=CLIENT_ID,
                FAKE_NOTION_CLIENT_SECRET=CLIENT_SECRET, FAKE_NOTION_DIR=fx)
    wait_until(_alive, timeout=30, what="fake Notion up")
    return procs


def make_api(monkeypatch, *, secret=CLIENT_SECRET, configured=True):
    monkeypatch.setenv("NOTION_API_BASE_URL", f"{NOTION}/v1")
    if configured:
        monkeypatch.setenv("NOTION_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("NOTION_CLIENT_SECRET", secret)
        monkeypatch.setenv("NOTION_REDIRECT_URI", REDIRECT)
    else:
        for k in ("NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET", "NOTION_REDIRECT_URI"):
            monkeypatch.delenv(k, raising=False)
    from notesagent.gateway.main import create_app
    return TestClient(create_app())


@pytest.fixture
def api_oauth(monkeypatch, notion_server):
    with make_api(monkeypatch) as client:
        yield client


def exchanges():
    return httpx.get(f"{NOTION}/_oauth", timeout=5).json()


def start(api):
    r = api.post("/oauth/notion/authorize")
    assert r.status_code == 200, r.text
    return r.json()["state"]


# ===================================================================== authorize
def test_authorize_returns_notion_url_and_single_use_state(api_oauth):
    r = api_oauth.post("/oauth/notion/authorize")
    body = r.json()
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    parts = urlsplit(body["authorization_url"])
    q = parse_qs(parts.query)
    assert parts.path == "/v1/oauth/authorize"
    assert q == {"client_id": [CLIENT_ID], "response_type": ["code"], "owner": ["user"],
                 "redirect_uri": [REDIRECT], "state": [body["state"]]}
    assert len(body["state"]) >= 40 and body["redirect_uri"] == REDIRECT and body["expires_in"] == 600
    r2 = redis.Redis.from_url(REDIS_URL)
    key = "oauth:notion:state:" + hashlib.sha256(body["state"].encode()).hexdigest()
    assert r2.exists(key) and 0 < r2.ttl(key) <= 600
    assert not [k for k in r2.keys("oauth:*") if body["state"].encode() in k]          # only the hash is stored
    assert api_oauth.post("/oauth/notion/authorize").json()["state"] != body["state"]


def test_not_configured_gateway_refuses(monkeypatch, notion_server):
    with make_api(monkeypatch, configured=False) as api:
        for path in ("/oauth/notion/authorize", "/oauth/notion/callback"):
            r = api.post(path, json={"state": "x", "code": "good-1"})
            assert (r.status_code, r.json()["error"]["code"]) == (503, "OAUTH_NOT_CONFIGURED")


# ===================================================================== callback success + sealed token
def test_callback_exchanges_the_code_and_returns_only_a_sealed_token(api_oauth):
    state = start(api_oauth)
    r = api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "good-code-1"})
    body = r.json()
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert body["notion_token"].startswith("nva1.") and body["token_format"] == "sealed-v1"
    assert (body["workspace_id"], body["workspace_name"], body["bot_id"]) == ("ws-456", "Test Workspace", "bot-123")
    assert "ntn_fake_access_good-code-1" not in r.text and "nrt_fake" not in r.text      # raw tokens never returned
    [ex] = exchanges()
    assert ex == {"basic_ok": True, "grant_type": "authorization_code", "code": "good-code-1", "redirect_uri": REDIRECT}
    assert redis.Redis.from_url(REDIS_URL).exists("ratelimit:notion:next:oauth-token-exchange")   # permit taken


def test_sealed_token_reaches_the_job_mailbox_as_the_real_token(api_oauth, procs, fx, db):
    state = start(api_oauth)
    sealed = api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "good-code-2"}).json()["notion_token"]
    r = api_oauth.post("/jobs", json={"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "notion_token": sealed})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    drain_outbox()
    procs.start("worker", "tests.support.run_fixture_worker", FIXTURE_DIR=fx, FIXTURE_TOKEN_STAGE="TRANSCRIBING")
    hashes = wait_until(lambda: list(fx.glob(f"tokenhash.{job_id}.*")), timeout=30, what="worker read the token")
    assert hashes[0].read_text() == hashlib.sha256(b"ntn_fake_access_good-code-2").hexdigest()


def test_tampered_sealed_token_is_rejected_on_submission(api_oauth):
    state = start(api_oauth)
    sealed = api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "good-3"}).json()["notion_token"]
    for bad in (sealed[:-6] + "AAAAAA", "nva1.not-a-real-sealed-token"):
        r = api_oauth.post("/jobs", json={"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "notion_token": bad})
        assert (r.status_code, r.json()["error"]["code"]) == (400, "INVALID_NOTION_TOKEN")


# ===================================================================== state handling
def test_state_is_single_use(api_oauth):
    state = start(api_oauth)
    assert api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "good-4"}).status_code == 200
    r = api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "good-4"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "OAUTH_STATE_INVALID") and len(exchanges()) == 1


def test_unknown_and_expired_state_are_rejected(api_oauth):
    r = api_oauth.post("/oauth/notion/callback", json={"state": "never-issued", "code": "good-5"})
    assert r.json()["error"]["code"] == "OAUTH_STATE_INVALID"
    state = start(api_oauth)
    redis.Redis.from_url(REDIS_URL).pexpire("oauth:notion:state:" + hashlib.sha256(state.encode()).hexdigest(), 50)
    import time
    time.sleep(0.3)
    r = api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "good-5"})
    assert r.json()["error"]["code"] == "OAUTH_STATE_INVALID" and exchanges() == []


@pytest.mark.parametrize("payload,code", [({}, "OAUTH_INVALID_REQUEST"), ({"code": "good-6"}, "OAUTH_INVALID_REQUEST"),
                                          ("not json", "OAUTH_INVALID_REQUEST")])
def test_malformed_callbacks(api_oauth, payload, code):
    kwargs = {"content": payload} if isinstance(payload, str) else {"json": payload}
    r = api_oauth.post("/oauth/notion/callback", **kwargs)
    assert (r.status_code, r.json()["error"]["code"]) == (400, code)


def test_missing_code_consumes_the_state(api_oauth):
    state = start(api_oauth)
    r = api_oauth.post("/oauth/notion/callback", json={"state": state})
    assert r.json()["error"]["code"] == "OAUTH_CODE_MISSING"
    assert api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "good-7"}).json()["error"]["code"] == \
        "OAUTH_STATE_INVALID"


def test_denied_authorization(api_oauth):
    state = start(api_oauth)
    r = api_oauth.post("/oauth/notion/callback", json={"state": state, "error": "access_denied"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "OAUTH_ACCESS_DENIED") and exchanges() == []
    state = start(api_oauth)
    r = api_oauth.post("/oauth/notion/callback", json={"state": state, "error": "server_error"})
    assert r.json()["error"]["code"] == "OAUTH_AUTHORIZATION_FAILED"


# ===================================================================== exchange failures
@pytest.mark.parametrize("auth_code,status,code", [("expired", 400, "OAUTH_EXCHANGE_FAILED"),
                                                   ("boom500", 502, "NOTION_UNAVAILABLE"),
                                                   ("ratelimit", 503, "NOTION_RATE_LIMITED")])
def test_exchange_failures(api_oauth, auth_code, status, code):
    state = start(api_oauth)
    r = api_oauth.post("/oauth/notion/callback", json={"state": state, "code": auth_code})
    assert (r.status_code, r.json()["error"]["code"]) == (status, code)
    if code == "NOTION_RATE_LIMITED":
        assert r.headers["retry-after"] == "7"
    assert "notion_token" not in r.json()


def test_wrong_client_secret_is_reported_without_leaking_it(monkeypatch, notion_server):
    with make_api(monkeypatch, secret="a-wrong-secret-value-1234") as api:
        state = start(api)
        r = api.post("/oauth/notion/callback", json={"state": state, "code": "good-8"})
        assert (r.status_code, r.json()["error"]["code"]) == (502, "OAUTH_CLIENT_REJECTED")
        assert "a-wrong-secret-value-1234" not in r.text


def test_notion_unreachable(api_oauth, notion_server):
    state = start(api_oauth)
    notion_server.procs["notion"].kill()
    notion_server.procs["notion"].wait(timeout=10)
    r = api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "good-9"})
    assert (r.status_code, r.json()["error"]["code"]) == (502, "NOTION_UNREACHABLE")


# ===================================================================== no secrets anywhere
def test_no_secret_code_or_token_in_responses_or_logs(api_oauth, capfd):
    texts = []
    state = start(api_oauth)
    texts.append(api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "good-secret-code-10"}).text)
    state = start(api_oauth)
    texts.append(api_oauth.post("/oauth/notion/callback", json={"state": state, "code": "expired"}).text)
    out, err = capfd.readouterr()
    everything = "\n".join(texts) + out + err
    for secret in (CLIENT_SECRET, "good-secret-code-10", "ntn_fake_access_", "nrt_fake_", state):
        assert secret not in everything, secret
