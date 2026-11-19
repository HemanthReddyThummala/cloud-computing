"""TEST-ONLY, OBSERVATIONAL failure diagnostics (generalises the mailbox-test pattern).

A ``TestDiagnostics`` object is created for every test that requests the ``diag`` fixture (see
conftest). Tests add milestones (``diag.mark``) and tracked job ids (``diag.track_job``). When the
test fails in setup, call or teardown, conftest's report hook calls ``collect()`` and the text is
attached to pytest's failure report AND written to a file under DIAG_DIR (bind-mounted to the host).

Collection is strictly read-only: SQL SELECTs, RabbitMQ management-API GETs, Redis INFO, file
listings and log reads. No AMQP declares, no writes. Every section is collected independently and
can never raise (a failing section reports its own error type instead).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
import redis
from psycopg.rows import dict_row

VHOST = "notes_test"
_LOG_TAIL = 40
_LINE_MAX = 300


def _section(title: str, fn) -> str:
    try:
        body = fn()
    except Exception as exc:  # noqa: BLE001 - diagnostics must never raise
        body = f"(section unavailable: {type(exc).__name__}: {str(exc)[:200]})"
    return f"\n----- {title} -----\n{body}"


def _short(value: Any) -> str:
    text = json.dumps(value, default=str, separators=(",", ":")) if not isinstance(value, str) else value
    return text if len(text) <= _LINE_MAX else text[:_LINE_MAX] + "…"


class TestDiagnostics:
    __test__ = False  # not a pytest test class

    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.started_wall = time.time()
        self._t0 = time.monotonic()
        self.marks: list[tuple[str, float]] = [("test setup started", 0.0)]
        self.job_ids: list[str] = []
        self.procs = None
        self.fx: Path | None = None

    # ------------------------------------------------------------ recording (called by tests)
    def attach(self, *, procs=None, fx: Path | None = None) -> "TestDiagnostics":
        self.procs, self.fx = procs, fx
        self.mark("diag fixture ready (test body starts)")
        return self

    def mark(self, label: str) -> None:
        self.marks.append((label, time.monotonic() - self._t0))

    def track_job(self, job_id: str) -> None:
        if job_id not in self.job_ids:
            self.job_ids.append(job_id)
        self.mark(f"job tracked {job_id}")

    # ------------------------------------------------------------ collection (on failure only)
    def collect(self, *, phase: str, error_text: str = "") -> str:
        now = time.time()
        header = (f"DIAGNOSTICS for {self.nodeid}\nphase={phase}  "
                  f"started={datetime.fromtimestamp(self.started_wall, timezone.utc).isoformat()}  "
                  f"collected={datetime.fromtimestamp(now, timezone.utc).isoformat()}  "
                  f"elapsed={now - self.started_wall:.2f}s  jobs={self.job_ids}")
        parts = [header, _section("milestones (t+seconds, Δ since previous)", self._milestones)]
        for job_id in self.job_ids:
            parts.append(_section(f"job {job_id}: row / retry state / counters", lambda j=job_id: self._job(j)))
            parts.append(_section(f"job {job_id}: event timeline", lambda j=job_id: self._events(j)))
            parts.append(_section(f"job {job_id}: outbox rows", lambda j=job_id: self._outbox(j)))
            parts.append(_section(f"job {job_id}: checkpoints", lambda j=job_id: self._checkpoints(j)))
        parts += [
            _section("RabbitMQ queues (management API, stats may lag ~5 s)", self._queues),
            _section("RabbitMQ node health (uptime ⇒ restarts, memory, alarms)", self._rabbit_node),
            _section("PostgreSQL health (start time ⇒ restarts)", self._postgres),
            _section("Redis health", self._redis),
            _section("fixture markers (run/started/hook files; t+ from test start)", self._fixture_files),
            _section("worker / helper processes (return code -9 = SIGKILL)", self._processes),
        ]
        if error_text:
            parts.append(_section("pytest failure (traceback kept verbatim)", lambda: error_text))
        return "\n".join(parts) + "\n"

    def write(self, text: str, phase: str) -> str:
        directory = Path(os.environ.get("DIAG_DIR", "/app/test-artifacts/diagnostics"))
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            directory = Path("/tmp/diagnostics")
            directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.nodeid.split("::")[-1])[:80]
        path = directory / f"{stamp}_{safe}_{phase}.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    # ------------------------------------------------------------ sections
    def _milestones(self) -> str:
        lines, previous = [], 0.0
        for label, at in self.marks:
            lines.append(f"t+{at:8.3f}s  (Δ {at - previous:7.3f}s)  {label}")
            previous = at
        return "\n".join(lines)

    @staticmethod
    def _db():
        return psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True, row_factory=dict_row,
                               connect_timeout=5)

    def _job(self, job_id: str) -> str:
        with self._db() as conn:
            row = conn.execute("SELECT status, progress, attempts, infra_attempts, retry, error, result,"
                               " created_at, updated_at, finished_at FROM jobs WHERE id = %s", (job_id,)).fetchone()
        return "(no such job)" if row is None else "\n".join(f"{k}: {_short(v)}" for k, v in row.items())

    def _events(self, job_id: str) -> str:
        with self._db() as conn:
            rows = conn.execute("SELECT event_type, from_status, to_status, worker_id, detail, created_at"
                                " FROM job_events WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()
        if not rows:
            return "(no events)"
        t0 = rows[0]["created_at"]
        return "\n".join(f"+{(r['created_at'] - t0).total_seconds():7.3f}s {r['event_type']:<20} "
                         f"{r['from_status']}->{r['to_status']} worker={r['worker_id']} {_short(r['detail'])}"
                         for r in rows)

    def _outbox(self, job_id: str) -> str:
        with self._db() as conn:
            rows = conn.execute("SELECT event_id, event_type, destination_exchange, destination_routing_key,"
                                " created_at, published_at, publish_attempts, last_error FROM outbox"
                                " WHERE job_id = %s ORDER BY id", (job_id,)).fetchall()
        return "\n".join(_short(dict(r)) for r in rows) or "(no outbox rows)"

    def _checkpoints(self, job_id: str) -> str:
        with self._db() as conn:
            rows = conn.execute("SELECT stage, worker_id, created_at FROM job_checkpoints WHERE job_id = %s",
                                (job_id,)).fetchall()
        return "\n".join(_short(dict(r)) for r in rows) or "(no checkpoints)"

    @staticmethod
    def _mgmt():
        return httpx.Client(base_url=os.environ["TEST_RABBITMQ_MGMT_URL"], timeout=5,
                            auth=(os.environ["TEST_RABBITMQ_USER"], os.environ["TEST_RABBITMQ_PASS"]))

    def _queues(self) -> str:
        with self._mgmt() as client:
            resp = client.get(f"/api/queues/{VHOST}", params={
                "columns": "name,type,messages,messages_ready,messages_unacknowledged,consumers,policy"})
        if resp.status_code == 404:
            return "(vhost not found)"
        resp.raise_for_status()
        wanted = [q for q in resp.json() if q["name"].startswith(("jobs.", "cred."))]
        return "\n".join(_short(q) for q in sorted(wanted, key=lambda q: q["name"])) or "(no queues)"

    def _rabbit_node(self) -> str:
        with self._mgmt() as client:
            nodes = client.get("/api/nodes", params={
                "columns": "name,uptime,mem_used,mem_limit,mem_alarm,disk_free_alarm,fd_used,proc_used,running"
            }).json()
        return "\n".join(_short({**n, "uptime_s": round(n.get("uptime", 0) / 1000, 1)}) for n in nodes)

    def _postgres(self) -> str:
        with self._db() as conn:
            row = conn.execute("SELECT pg_postmaster_start_time() AS started, now() AS now,"
                               " (SELECT count(*) FROM pg_stat_activity) AS connections").fetchone()
        return _short(dict(row))

    @staticmethod
    def _redis() -> str:
        info = redis.Redis.from_url(os.environ["TEST_REDIS_URL"], socket_timeout=3).info("server")
        return _short({"uptime_in_seconds": info.get("uptime_in_seconds"), "redis_version": info.get("redis_version")})

    def _fixture_files(self) -> str:
        if self.fx is None or not self.fx.exists():
            return "(no fixture directory)"
        entries = sorted(self.fx.iterdir(), key=lambda p: p.stat().st_mtime)
        return "\n".join(f"t+{p.stat().st_mtime - self.started_wall:8.3f}s  {p.name}" for p in entries) or "(empty)"

    def _processes(self) -> str:
        if self.procs is None:
            return "(no processes started)"
        lines = []
        for name, proc in self.procs.procs.items():
            lines.append(f"=== {name}: pid={proc.pid} returncode={proc.poll()}")
            tail = self.procs.log(name).splitlines()[-_LOG_TAIL:]
            lines += [f"    {line[:_LINE_MAX]}" for line in tail]
        return "\n".join(lines) or "(none)"
