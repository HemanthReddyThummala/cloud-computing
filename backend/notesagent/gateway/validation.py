"""Request parsing that never echoes user input (tokens!) back in error messages."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from notesagent.common.errors import ApiError
from notesagent.gateway.youtube import InvalidYouTubeUrl, extract_video_id

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,128}$")
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,1024}$")  # printable ASCII, no whitespace


class SubmitJobBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    youtube_url: str = Field(min_length=1, max_length=2048)
    notion_token: SecretStr
    notion_parent_id: str | None = Field(default=None, max_length=64)


@dataclass(frozen=True)
class SubmitJobRequest:
    youtube_url: str
    video_id: str
    notion_token: str
    notion_parent_id: uuid.UUID | None


async def read_json_body(request: Request, max_bytes: int) -> object:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json.")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"Request body must be at most {max_bytes} bytes.")
    raw = await request.body()
    if len(raw) > max_bytes:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", f"Request body must be at most {max_bytes} bytes.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(400, "INVALID_JSON", "Request body is not valid JSON.") from exc


def _describe(exc: ValidationError) -> str:
    """Field path + generic reason only — Pydantic's ``input`` is deliberately dropped."""
    first = exc.errors(include_input=False, include_url=False)[0]
    location = ".".join(str(p) for p in first.get("loc", ())) or "body"
    return f"Invalid field '{location}': {first.get('msg', 'invalid value')}."


def parse_submit_request(payload: object) -> SubmitJobRequest:
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_REQUEST", "Request body must be a JSON object.")
    try:
        body = SubmitJobBody.model_validate(payload)
    except ValidationError as exc:
        raise ApiError(400, "INVALID_REQUEST", _describe(exc)) from None

    token = body.notion_token.get_secret_value()
    if not _TOKEN_RE.fullmatch(token):
        raise ApiError(400, "INVALID_NOTION_TOKEN", "notion_token must be a non-empty string without whitespace.")
    try:
        video_id = extract_video_id(body.youtube_url)
    except InvalidYouTubeUrl as exc:
        raise ApiError(400, "INVALID_YOUTUBE_URL", f"youtube_url is not a valid YouTube video URL ({exc}).") from None

    parent_id: uuid.UUID | None = None
    if body.notion_parent_id:
        try:
            parent_id = uuid.UUID(body.notion_parent_id)
        except ValueError:
            raise ApiError(400, "INVALID_NOTION_PARENT_ID",
                           "notion_parent_id must be a Notion page id (32 hex characters, dashes optional).") from None

    return SubmitJobRequest(youtube_url=body.youtube_url, video_id=video_id, notion_token=token,
                            notion_parent_id=parent_id)


def validate_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if not IDEMPOTENCY_KEY_RE.fullmatch(value):
        raise ApiError(400, "INVALID_IDEMPOTENCY_KEY",
                       "Idempotency-Key must be 8-128 characters of letters, digits, '-', '_', ':' or '.'.")
    return value


def parse_job_id(value: str) -> uuid.UUID:
    """Malformed ids are simply unknown jobs -> 404."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ApiError(404, "JOB_NOT_FOUND", "No job with this id exists.") from None
