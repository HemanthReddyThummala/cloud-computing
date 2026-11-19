"""GET /healthz honesty: real dependencies, and deliberately unreachable ones."""
import os
import uuid
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from notesagent.common.config import get_settings
from notesagent.gateway.main import create_app
from tests.support.helpers import count, submit, wait_until


def _passwords() -> list[str]:
    return [urlsplit(os.environ[v]).password for v in ("TEST_DATABASE_URL", "TEST_RABBITMQ_URL")]


def _assert_no_secrets(text: str) -> None:
    for pw in _passwords():
        assert pw not in text


def test_healthz_all_dependencies_up(api):
    resp = api.get("/healthz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    for name in ("gateway", "postgres", "rabbitmq", "redis"):
        assert body["checks"][name]["status"] == "ok"
    assert body["checks"]["postgres"]["outbox_pending"] == 0
    _assert_no_secrets(resp.text)


def _unreachable(url_var: str, port: int) -> str:
    parts = urlsplit(os.environ[url_var])
    return parts._replace(netloc=parts.netloc.rsplit(":", 1)[0] + f":{port}").geturl()


@pytest.mark.parametrize("override,down", [
    ({"redis_url": "redis://redis:6390/15"}, "redis"),
    ({"rabbitmq_url": "RABBIT"}, "rabbitmq"),
    ({"database_url": "DB"}, "postgres"),
])
def test_healthz_reports_unavailable_dependency(override, down):
    if override.get("rabbitmq_url") == "RABBIT":
        override = {"rabbitmq_url": _unreachable("TEST_RABBITMQ_URL", 5679)}
    if override.get("database_url") == "DB":
        override = {"database_url": _unreachable("TEST_DATABASE_URL", 5439)}
    with TestClient(create_app(get_settings(**override))) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unavailable" and body["checks"][down]["status"] == "down"
    for name in {"postgres", "rabbitmq", "redis"} - {down}:
        assert body["checks"][name]["status"] == "ok", body
    _assert_no_secrets(resp.text)


def test_submit_returns_503_when_broker_down(db):
    settings = get_settings(rabbitmq_url=_unreachable("TEST_RABBITMQ_URL", 5679))
    with TestClient(create_app(settings)) as client:
        resp = submit(client)
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "BROKER_UNAVAILABLE"
    assert count(db, "jobs") == 0
    _assert_no_secrets(resp.text)


def test_submit_returns_503_and_discards_mailbox_when_database_down(rabbit_admin):
    settings = get_settings(database_url=_unreachable("TEST_DATABASE_URL", 5439))
    with TestClient(create_app(settings)) as client:
        resp = submit(client)
        assert client.get(f"/jobs/{uuid.uuid4()}").status_code == 503
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert not [q for q in rabbit_admin.queue_names() if q.startswith("cred.")]


def test_healthz_reports_live_workers(api, procs, fx):
    procs.start("worker-a", "tests.support.run_fixture_worker", FIXTURE_DIR=fx)
    procs.start("worker-b", "tests.support.run_fixture_worker", FIXTURE_DIR=fx)
    wait_until(lambda: api.get("/healthz").json()["checks"]["redis"].get("live_workers") == 2,
               timeout=30, what="two live workers")
    assert api.get("/healthz").json()["checks"]["rabbitmq"]["work_queue_consumers"] == 2


def test_livez(api):
    assert api.get("/livez").json() == {"status": "alive"}
