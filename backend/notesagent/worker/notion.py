"""Notion REST client for the PUBLISHING stage (ADR-07, ADR-17).

- A ``Provider.NOTION`` permit from the shared limiter (ADR-13) is taken before EVERY request; the
  limiter's connection key is a hash of the token, never the token itself.
- No retries here: errors are classified (ADR-10, ``classify_http`` with the HTTP method, so a failed
  write is marked ``notion_write_outcome_unknown``) and retried only by the outbox system (ADR-11).
  Because a write's outcome can be unknown, the stage reconciles (reads) before writing again.
- The token is only placed in the Authorization header; it is never logged or put in errors.
"""
from __future__ import annotations

import hashlib
from typing import Any

import httpx

from notesagent.common.config import Settings
from notesagent.common.errors import PipelineError, Provider, TransientError
from notesagent.common.http_errors import classify_http

P = Provider.NOTION
_SAFE = {"GET", "HEAD", "OPTIONS"}


def connection_key(token: str) -> str:
    """Limiter key for one Notion connection (per authorization), derived from — not containing — the token."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class NotionClient:
    def __init__(self, settings: Settings, token: str, limiter, *, transport: httpx.AsyncBaseTransport | None = None):
        self.limiter = limiter
        self.connection = connection_key(token)
        self._http = httpx.AsyncClient(
            base_url=settings.notion_api_base_url.rstrip("/") + "/",
            timeout=settings.notion_request_timeout_sec, transport=transport,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": settings.notion_version})

    def __repr__(self) -> str:                                     # never reveal the token
        return "NotionClient()"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                      files: dict | None = None) -> dict[str, Any]:
        await self.limiter.acquire(P, connection=self.connection)    # BEFORE every Notion request
        try:
            response = await self._http.request(method, path.lstrip("/"), json=json, params=params, files=files)
        except httpx.TransportError as exc:
            raise TransientError("NOTION_UNREACHABLE", "Notion could not be reached.", provider=P,
                                 detail={"exception_type": type(exc).__name__,
                                         "notion_write_outcome_unknown": method.upper() not in _SAFE}) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 300:
            error = classify_http(P, response.status_code, response.headers, body, method=method)
            raise (error or TransientError("NOTION_UNEXPECTED_RESPONSE", "Notion returned an unexpected response.",
                                           provider=P)) from None
        if not isinstance(body, dict):
            raise TransientError("NOTION_UNEXPECTED_RESPONSE", "Notion returned an unexpected response.", provider=P)
        return body

    # ---------------------------------------------------------------- endpoints used by PUBLISHING
    async def list_children(self, block_id: str, cursor: str | None = None, page_size: int = 100) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["start_cursor"] = cursor
        return await self.request("GET", f"blocks/{block_id}/children", params=params)

    async def all_children(self, block_id: str) -> list[dict[str, Any]]:
        blocks, cursor = [], None
        while True:
            page = await self.list_children(block_id, cursor)
            blocks.extend(page.get("results") or [])
            if not page.get("has_more"):
                return blocks
            cursor = page.get("next_cursor")

    async def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return await self.request("GET", f"pages/{page_id}")

    async def create_page(self, parent_id: str, title_rich_text: list[dict], children: list[dict]) -> dict[str, Any]:
        return await self.request("POST", "pages", json={
            "parent": {"page_id": parent_id},
            "properties": {"title": {"title": title_rich_text}},
            "children": children})

    async def append_children(self, block_id: str, children: list[dict]) -> dict[str, Any]:
        return await self.request("PATCH", f"blocks/{block_id}/children", json={"children": children})

    async def upload_image(self, filename: str, data: bytes) -> str:
        """File Upload API (single part): create, then send. Returns the file-upload id."""
        created = await self.request("POST", "file_uploads", json={"filename": filename, "content_type": "image/jpeg"})
        upload_id = created.get("id")
        if not isinstance(upload_id, str):
            raise TransientError("NOTION_UNEXPECTED_RESPONSE", "Notion returned an unexpected response.", provider=P)
        await self.request("POST", f"file_uploads/{upload_id}/send", files={"file": (filename, data, "image/jpeg")})
        return upload_id


def is_permanent(error: PipelineError) -> bool:
    return not error.retryable
