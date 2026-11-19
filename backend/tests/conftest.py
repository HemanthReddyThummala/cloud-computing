"""Test configuration.

Integration tests run against REAL PostgreSQL, RabbitMQ and Redis containers, isolated from
the development stack: database ``notes_test`` (recreated each session), RabbitMQ vhost
``notes_test`` (recreated each test) and Redis db 15 (flushed each test). A throwaway
encryption key is generated per session; the real key in .env is never used by tests.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

# Point every component at the isolated test infrastructure BEFORE importing app code.
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["RABBITMQ_URL"] = os.environ["TEST_RABBITMQ_URL"]
os.environ["REDIS_URL"] = os.environ["TEST_REDIS_URL"]
os.environ["JOB_PAYLOAD_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["RABBITMQ_MANAGEMENT_URL"] = os.environ["TEST_RABBITMQ_MGMT_URL"]
os.environ["BACKOFF_SCHEDULE_SEC"] = "2,4,8"
os.environ["LEGACY_DRAIN_INTERVAL_SEC"] = "0.5"
os.environ["WORKER_HEARTBEAT_FILE"] = "/tmp/test-worker-heartbeat"
os.environ["OUTBOX_HEARTBEAT_FILE"] = "/tmp/test-outbox-heartbeat"

import psycopg  # noqa: E402
import pytest  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from notesagent.common.migrate import apply_migrations  # noqa: E402
from tests.support.diagnostics import TestDiagnostics  # noqa: E402
from tests.support.helpers import TABLES, connect  # noqa: E402
from tests.support.processes import Processes  # noqa: E402
from tests.support.rabbit import RabbitAdmin  # noqa: E402


@pytest.fixture(scope="session")
def rabbit_admin() -> RabbitAdmin:
    return RabbitAdmin()


@pytest.fixture(scope="session", autouse=True)
def test_database():
    with psycopg.connect(os.environ["TEST_ADMIN_DATABASE_URL"], autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS notes_test WITH (FORCE)")
        admin.execute("CREATE DATABASE notes_test")
    applied = apply_migrations(os.environ["TEST_DATABASE_URL"])
    assert applied, "migrations should apply to a fresh database"
    yield


@pytest.fixture(autouse=True)
def clean_state(request, rabbit_admin):
    if request.module.__name__.rsplit(".", 1)[-1].startswith("test_unit"):
        yield
        return
    with connect() as db:
        db.execute(f"TRUNCATE {', '.join(TABLES)} CASCADE")
    rabbit_admin.reset_vhost()
    redis.Redis.from_url(os.environ["TEST_REDIS_URL"]).flushdb()
    yield


@pytest.fixture
def db():
    with connect() as conn:
        yield conn


@pytest.fixture
def api():
    from notesagent.gateway.main import create_app
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def procs(tmp_path):
    processes = Processes(tmp_path / "logs")
    yield processes
    processes.stop_all()


@pytest.fixture
def fx(tmp_path):
    path = tmp_path / "fixture"
    path.mkdir()
    return path


# ---------------------------------------------------------------- observational failure diagnostics
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    """Create the collector BEFORE any fixture runs, so even setup errors (e.g. clean_state) are covered."""
    if "diag" in getattr(item, "fixturenames", ()):
        item._diag = TestDiagnostics(item.nodeid)
    yield


@pytest.fixture
def diag(request, procs, fx):
    """Opt-in diagnostics: milestones + tracked jobs; collected only if the test fails."""
    collector = getattr(request.node, "_diag", None) or TestDiagnostics(request.node.nodeid)
    request.node._diag = collector
    return collector.attach(procs=procs, fx=fx)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """On failure in setup/call/teardown of a ``diag`` test: attach diagnostics to pytest's report
    (pytest's own traceback is untouched) and write them to DIAG_DIR. Passing runs add nothing."""
    outcome = yield
    report = outcome.get_result()
    collector = getattr(item, "_diag", None)
    if collector is None or not report.failed:
        return
    try:
        collector.mark(f"failure detected in {report.when}")
        text = collector.collect(phase=report.when, error_text=report.longreprtext)
        path = collector.write(text, report.when)
        report.sections.append((f"diagnostics ({report.when}) — saved to {path}", text))
    except Exception as exc:  # noqa: BLE001 - never let diagnostics mask the real failure
        report.sections.append(("diagnostics", f"collection failed: {type(exc).__name__}"))
