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
os.environ["LOCK_BUSY_RETRY_DELAY_SEC"] = "0.5"
os.environ["UNEXPECTED_ERROR_RETRY_DELAY_SEC"] = "0.2"
os.environ["WORKER_HEARTBEAT_FILE"] = "/tmp/test-worker-heartbeat"
os.environ["OUTBOX_HEARTBEAT_FILE"] = "/tmp/test-outbox-heartbeat"

import psycopg  # noqa: E402
import pytest  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from notesagent.common.migrate import apply_migrations  # noqa: E402
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
