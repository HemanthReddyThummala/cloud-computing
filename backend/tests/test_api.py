"""REST API contract (spec §5) against real PostgreSQL + RabbitMQ."""
import uuid

import pytest

from notesagent.common.states import JobStatus as S
from tests.support.helpers import (
    VIDEO_ID,
    VIDEO_URL,
    count,
    dump_all_tables,
    events,
    force_transitions,
    job,
    new_token,
    submit,
    submit_ok,
    token_secret_part,
)
from tests.support.rabbit import queue_state


def _assert_error(resp, status, code):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body["error"]) == {"code", "message"} and body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


# ---------------------------------------------------------------- POST /jobs
def test_submit_returns_202_location_and_body(api, db):
    token = new_token()
    resp = submit(api, token=token)
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"job_id", "status", "video_id"}
    assert body["status"] == "QUEUED" and body["video_id"] == VIDEO_ID
    uuid.UUID(body["job_id"])
    assert resp.headers["location"] == f"/jobs/{body['job_id']}"

    row = job(db, body["job_id"])
    assert row["status"] == "QUEUED" and row["progress"] == 0 and row["youtube_url"] == VIDEO_URL
    outbox = db.execute("SELECT * FROM outbox WHERE job_id = %s", (body["job_id"],)).fetchall()
    assert len(outbox) == 1 and outbox[0]["published_at"] is None
    assert outbox[0]["payload"]["job_id"] == body["job_id"]
    assert [e["event_type"] for e in events(db, body["job_id"])] == ["submitted"]
    # ADR-03: the token is not stored anywhere in PostgreSQL.
    assert token_secret_part(token) not in dump_all_tables(db)


def test_submit_does_no_pipeline_work(api, db):
    job_id = submit_ok(api)
    assert job(db, job_id)["status"] == "QUEUED"
    assert count(db, "job_checkpoints") == 0
    # Nothing reaches the work queue until the outbox publisher runs (separate process).
    assert queue_state("jobs.work.v2").messages == 0


def test_submit_with_parent_id(api, db):
    parent = uuid.uuid4()
    job_id = submit_ok(api, parent=parent.hex)
    assert job(db, job_id)["notion_parent_id"] == parent


INVALID = [
    ({"youtube_url": "https://vimeo.com/76979871", "notion_token": "TOKEN"}, "INVALID_YOUTUBE_URL"),
    ({"youtube_url": "https://www.youtube.com/watch?v=short", "notion_token": "TOKEN"}, "INVALID_YOUTUBE_URL"),
    ({"youtube_url": "javascript:alert(1)", "notion_token": "TOKEN"}, "INVALID_YOUTUBE_URL"),
    ({"youtube_url": "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ", "notion_token": "TOKEN"},
     "INVALID_YOUTUBE_URL"),
    ({"youtube_url": VIDEO_URL}, "INVALID_REQUEST"),
    ({"notion_token": "TOKEN"}, "INVALID_REQUEST"),
    ({"youtube_url": VIDEO_URL, "notion_token": "TOKEN", "unexpected": 1}, "INVALID_REQUEST"),
    ({"youtube_url": 12345, "notion_token": "TOKEN"}, "INVALID_REQUEST"),
    ({"youtube_url": VIDEO_URL, "notion_token": "TOKEN with spaces"}, "INVALID_NOTION_TOKEN"),
    ({"youtube_url": VIDEO_URL, "notion_token": "TOKEN", "notion_parent_id": "not-a-page"},
     "INVALID_NOTION_PARENT_ID"),
]


@pytest.mark.parametrize("payload,code", INVALID)
def test_invalid_submissions_rejected_without_side_effects(api, db, rabbit_admin, payload, code):
    token = new_token()
    payload = {k: (v.replace("TOKEN", token) if isinstance(v, str) else v) for k, v in payload.items()}
    resp = api.post("/jobs", json=payload)
    _assert_error(resp, 400, code)
    assert token_secret_part(token) not in resp.text
    assert count(db, "jobs") == 0 and count(db, "outbox") == 0
    assert not [q for q in rabbit_admin.queue_names() if q.startswith("cred.")]


def test_non_object_json_rejected(api):
    _assert_error(api.post("/jobs", json=[1, 2]), 400, "INVALID_REQUEST")


def test_malformed_json_rejected(api):
    resp = api.post("/jobs", content=b'{"youtube_url": ', headers={"content-type": "application/json"})
    _assert_error(resp, 400, "INVALID_JSON")


def test_wrong_content_type_rejected(api):
    resp = api.post("/jobs", content=b"youtube_url=x", headers={"content-type": "application/x-www-form-urlencoded"})
    _assert_error(resp, 415, "UNSUPPORTED_MEDIA_TYPE")


def test_oversized_body_rejected(api, db):
    resp = api.post("/jobs", json={"youtube_url": VIDEO_URL, "notion_token": "x" * 20_000})
    _assert_error(resp, 413, "PAYLOAD_TOO_LARGE")
    assert count(db, "jobs") == 0


def test_invalid_idempotency_key_rejected(api, db):
    _assert_error(submit(api, key="bad key"), 400, "INVALID_IDEMPOTENCY_KEY")
    assert count(db, "jobs") == 0


# ---------------------------------------------------------------- GET /jobs/{id}
def test_get_job_returns_status_fields(api):
    job_id = submit_ok(api)
    resp = api.get(f"/jobs/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    for field in ("job_id", "status", "progress", "video_title", "duration_sec", "warnings", "error", "result"):
        assert field in body
    assert body["job_id"] == job_id and body["status"] == "QUEUED" and body["progress"] == 0
    assert body["warnings"] == [] and body["error"] is None and body["result"] is None


def test_get_job_is_read_only(api, db):
    job_id = submit_ok(api)
    before_row, before_events = job(db, job_id), len(events(db, job_id))
    for _ in range(5):
        assert api.get(f"/jobs/{job_id}").status_code == 200
        assert api.get(f"/jobs/{job_id}/results").status_code == 409
    assert job(db, job_id) == before_row
    assert len(events(db, job_id)) == before_events


@pytest.mark.parametrize("job_id", [str(uuid.uuid4()), "not-a-uuid", "1"])
def test_unknown_job_returns_404(api, job_id):
    _assert_error(api.get(f"/jobs/{job_id}"), 404, "JOB_NOT_FOUND")
    _assert_error(api.get(f"/jobs/{job_id}/results"), 404, "JOB_NOT_FOUND")


# ---------------------------------------------------------------- GET /jobs/{id}/results
@pytest.mark.parametrize("path", [[], [S.TRANSCRIBING], [S.TRANSCRIBING, S.SUMMARIZING, S.EXTRACTING_FRAMES,
                                                         S.PUBLISHING]])
def test_results_return_409_until_terminal(api, path):
    job_id = submit_ok(api)
    if path:
        force_transitions(job_id, path)
    resp = api.get(f"/jobs/{job_id}/results")
    _assert_error(resp, 409, "JOB_NOT_FINISHED")
    assert resp.json()["status"] == (path[-1].value if path else "QUEUED")


def test_results_for_failed_job(api):
    job_id = submit_ok(api)
    force_transitions(job_id, [S.TRANSCRIBING, S.FAILED],
                      error={"code": "VIDEO_TOO_LONG", "message": "Video exceeds 20 minutes."})
    resp = api.get(f"/jobs/{job_id}/results")
    assert resp.status_code == 200
    assert resp.json() == {"status": "FAILED", "result": None, "warnings": [],
                           "error": {"code": "VIDEO_TOO_LONG", "message": "Video exceeds 20 minutes."}}


def test_results_for_completed_job_follow_contract(api):
    job_id = submit_ok(api)
    result = {"notion_page_url": "https://www.notion.so/Example-0123456789abcdef0123456789abcdef",
              "frames_requested": 6, "frames_published": 5}
    warnings = [{"code": "FRAME_EXTRACTION_FAILED", "timestamp_sec": 461}]
    force_transitions(job_id, [S.TRANSCRIBING, S.SUMMARIZING, S.EXTRACTING_FRAMES, S.PUBLISHING,
                               S.COMPLETED_WITH_WARNINGS], result=result, warnings=warnings)
    body = api.get(f"/jobs/{job_id}/results").json()
    assert body["status"] == "COMPLETED_WITH_WARNINGS" and body["result"] == result
    assert body["warnings"] == warnings and body["error"] is None
    status = api.get(f"/jobs/{job_id}").json()
    assert status["progress"] == 100 and status["status"] == "COMPLETED_WITH_WARNINGS"


# ---------------------------------------------------------------- framework errors use the same format
def test_unknown_route_and_method_use_error_format(api):
    _assert_error(api.get("/nope"), 404, "NOT_FOUND")
    _assert_error(api.put("/jobs"), 405, "METHOD_NOT_ALLOWED")
