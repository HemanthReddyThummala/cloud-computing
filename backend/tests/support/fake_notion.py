"""TEST-ONLY fake Notion API (no network): the endpoints the PUBLISHING stage uses, with Notion's
limits (≤ 100 children per request, ≤ 2000 characters per rich-text item, uploaded file required
before attaching), a request log, and failure scenarios. State is inspected via GET /_state.

Usage: python -m tests.support.fake_notion <port>   (env FAKE_NOTION_DIR = fixture dir for gates)
In-process use (unit tests): httpx.ASGITransport(app=app); call reset() between tests.
Env:
  FAKE_NOTION   comma-separated flags:
                rate_limited_once      first request -> 429 with Retry-After: 1
                create_503_committed   first POST /pages is SAVED, then answered with 503
                gate_append            the 2nd PATCH children blocks until <fixture-dir>/release.notion exists
                gate_final_list        the first GET children of a created page (after its creation) blocks
                fail_append_503_once   the 2nd PATCH children is NOT saved and answered with 503
                upload_400_once        the first file send -> 400 validation_error
                unauthorized           every request -> 401
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
STATE: dict = {}


def reset() -> None:
    STATE.clear()
    STATE.update({"requests": [], "pages": {}, "children": {}, "uploads": {}, "tokens": set(), "counters": {}})


reset()


def flags() -> set[str]:
    return set(filter(None, os.environ.get("FAKE_NOTION", "").split(",")))


def fx_dir() -> Path:
    return Path(os.environ.get("FAKE_NOTION_DIR", "."))


def _err(status: int, code: str, message: str, headers: dict | None = None):
    return JSONResponse({"object": "error", "status": status, "code": code, "message": message},
                        status_code=status, headers=headers)


def _count(key: str) -> int:
    STATE["counters"][key] = STATE["counters"].get(key, 0) + 1
    return STATE["counters"][key]


async def _gate(kind: str) -> None:
    (fx_dir() / f"started.notion.{kind}").write_text("")
    while not (fx_dir() / "release.notion").exists():
        await asyncio.sleep(0.05)


def _with_plain(block: dict) -> dict:
    out = dict(block)
    content = dict(out.get(out.get("type"), {}) or {})
    if "rich_text" in content:
        content["rich_text"] = [{**r, "plain_text": (r.get("text") or {}).get("content", "")}
                                for r in content["rich_text"]]
    out[out.get("type")] = content
    return out


def _validate_children(children: list) -> JSONResponse | None:
    if len(children) > 100:
        return _err(400, "validation_error", "body.children.length should be ≤ 100")
    for b in children:
        content = b.get(b.get("type"), {}) or {}
        for r in content.get("rich_text", []) + content.get("caption", []):
            if len((r.get("text") or {}).get("content", "")) > 2000:
                return _err(400, "validation_error", "text.content.length should be ≤ 2000")
        if b.get("type") == "image":
            upload = STATE["uploads"].get((content.get("file_upload") or {}).get("id"))
            if not upload or upload["status"] != "uploaded":
                return _err(400, "validation_error", "file upload not found or not uploaded")
    return None


@app.middleware("http")
async def checks(request: Request, call_next):
    if request.url.path.startswith("/_"):
        return await call_next(request)
    STATE["requests"].append({"method": request.method, "path": request.url.path})
    auth = request.headers.get("authorization", "")
    STATE["tokens"].add(hashlib.sha256(auth.removeprefix("Bearer ").encode()).hexdigest())
    if not request.headers.get("notion-version"):
        return _err(400, "missing_version", "Notion-Version header missing")
    if "unauthorized" in flags():
        return _err(401, "unauthorized", "API token is invalid.")
    if "rate_limited_once" in flags() and _count("any") == 1:
        return _err(429, "rate_limited", "Rate limited", headers={"Retry-After": "1"})
    return await call_next(request)


@app.get("/_state")
async def state():
    return {"requests": STATE["requests"], "tokens": sorted(STATE["tokens"]),
            "pages": [{**p, "children": [_with_plain(b) for b in STATE["children"].get(p["id"], [])]}
                      for p in STATE["pages"].values()]}


@app.get("/v1/blocks/{block_id}/children")
async def list_children(block_id: str, page_size: int = 100, start_cursor: str | None = None):
    if block_id in STATE["pages"] and "gate_final_list" in flags() and _count(f"list:{block_id}") == 1:
        await _gate("final_list")
    if block_id not in STATE["children"]:
        STATE["children"][block_id] = []                    # any parent ID is a page shared with us
    blocks = STATE["children"][block_id]
    start = int(start_cursor or 0)
    page = blocks[start:start + page_size]
    more = start + page_size < len(blocks)
    return {"object": "list", "results": [_with_plain(b) for b in page], "has_more": more,
            "next_cursor": str(start + page_size) if more else None}


@app.get("/v1/pages/{page_id}")
async def retrieve_page(page_id: str):
    page = STATE["pages"].get(page_id)
    if page is None:
        return _err(404, "object_not_found", "Could not find page.")
    return {"object": "page", "id": page_id, "url": page["url"]}


@app.post("/v1/pages")
async def create_page(request: Request):
    body = await request.json()
    parent = (body.get("parent") or {}).get("page_id")
    title = "".join((r.get("text") or {}).get("content", "") for r in body["properties"]["title"]["title"])
    children = body.get("children") or []
    invalid = _validate_children(children)
    if invalid:
        return invalid
    page_id = str(uuid.uuid4())
    url = f"https://www.notion.so/Fake-{page_id.replace('-', '')}"
    STATE["pages"][page_id] = {"id": page_id, "parent": parent, "title": title, "url": url}
    STATE["children"].setdefault(parent, []).append(
        {"id": page_id, "type": "child_page", "child_page": {"title": title}})
    STATE["children"][page_id] = [{**b, "id": str(uuid.uuid4())} for b in children]
    if "create_503_committed" in flags() and _count("create") == 1:
        return _err(503, "service_unavailable", "Saved, but the response was lost.")
    return {"object": "page", "id": page_id, "url": url}


@app.patch("/v1/blocks/{block_id}/children")
async def append(block_id: str, request: Request):
    body = await request.json()
    n = _count("append")
    if "gate_append" in flags() and n == 2:
        await _gate("append")
    if "fail_append_503_once" in flags() and n == 2:
        return _err(503, "service_unavailable", "Not saved.")
    children = body.get("children") or []
    invalid = _validate_children(children)
    if invalid:
        return invalid
    STATE["children"].setdefault(block_id, []).extend({**b, "id": str(uuid.uuid4())} for b in children)
    return {"object": "list", "results": []}


@app.post("/v1/file_uploads")
async def create_upload(request: Request):
    upload_id = str(uuid.uuid4())
    STATE["uploads"][upload_id] = {"status": "pending"}
    return {"object": "file_upload", "id": upload_id, "status": "pending"}


@app.post("/v1/file_uploads/{upload_id}/send")
async def send_upload(upload_id: str, request: Request):
    raw = await request.body()
    if "upload_400_once" in flags() and _count("send") == 1:
        return _err(400, "validation_error", "The file could not be processed.")
    if upload_id not in STATE["uploads"] or b"\xff\xd8" not in raw:          # must contain JPEG data
        return _err(400, "validation_error", "invalid upload")
    STATE["uploads"][upload_id]["status"] = "uploaded"
    return {"object": "file_upload", "id": upload_id, "status": "uploaded"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
