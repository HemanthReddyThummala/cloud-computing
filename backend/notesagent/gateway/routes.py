"""REST endpoints (PROJECT_SPEC §5) plus /healthz and /livez."""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg_pool import PoolTimeout

from notesagent.common import jobs_repo, states
from notesagent.common.errors import ApiError
from notesagent.gateway.health import check_health
from notesagent.gateway.validation import (
    parse_job_id,
    parse_submit_request,
    read_json_body,
    validate_idempotency_key,
)

router = APIRouter()


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def job_status_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(row["id"]),
        "status": row["status"],
        "progress": row["progress"],
        "video_id": row["video_id"],
        "video_title": row["video_title"],
        "duration_sec": row["duration_sec"],
        "warnings": row["warnings"],
        "error": row["error"],
        "result": row["result"],
        "retry": row["retry"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def _load_job(request: Request, job_id: str) -> dict[str, Any]:
    uid = parse_job_id(job_id)
    try:
        async with request.app.state.pool.connection(timeout=5) as conn:
            row = await jobs_repo.get_job(conn, uid)  # SELECT only: polling never mutates state
    except (psycopg.Error, PoolTimeout):
        raise ApiError(503, "DATABASE_UNAVAILABLE", "The job store is unavailable. Please retry shortly.") from None
    if row is None:
        raise ApiError(404, "JOB_NOT_FOUND", "No job with this id exists.")
    return row


@router.post("/jobs", status_code=202)
async def submit_job(request: Request) -> JSONResponse:
    state = request.app.state
    idempotency_key = validate_idempotency_key(request.headers.get("idempotency-key"))
    payload = await read_json_body(request, state.settings.max_request_bytes)
    submit_request = parse_submit_request(payload)
    outcome = await state.submissions.submit(submit_request, idempotency_key)
    headers = {"Location": f"/jobs/{outcome.job_id}"}
    if outcome.replayed:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(outcome.body, status_code=202, headers=headers)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict[str, Any]:
    return job_status_view(await _load_job(request, job_id))


@router.get("/jobs/{job_id}/results")
async def get_job_results(job_id: str, request: Request) -> JSONResponse:
    row = await _load_job(request, job_id)
    if not states.is_terminal(row["status"]):
        raise ApiError(409, "JOB_NOT_FINISHED",
                       "The job has not reached a terminal state yet; poll GET /jobs/{job_id}.",
                       extra={"status": row["status"]})
    return JSONResponse({
        "status": row["status"],
        "result": row["result"],
        "warnings": row["warnings"],
        "error": row["error"],
    })


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    healthy, body = await check_health(request.app.state)
    return JSONResponse(body, status_code=200 if healthy else 503)


@router.get("/livez")
async def livez() -> dict[str, str]:
    """Process liveness only (used by the container healthcheck)."""
    return {"status": "alive"}
