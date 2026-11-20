"""TEST-ONLY fake Gemini client for the SUMMARIZING stage (no network).

Same interface as ``notesagent.worker.gemini.GeminiClient.generate``. Answers are built from the
segment numbers actually present in the prompt (``[#<n> mm:ss]``), so they are valid for any chunk.
Errors are the REAL google-genai / httpx2 exception classes. Each call writes
``FIXTURE_DIR/call.gemini.<kind>.<uuid>`` (kind = chunk | merge) with the segment range it saw.

Env:
  FAKE_GEMINI   ok (default) | invalid_json | invalid_json_once | schema_mismatch | blocked
                | rate_limited_once:<sec> | quota | server_once | network_once | leaky_once
                | ungrounded (segment numbers outside the prompt)
  FAKE_GEMINI_GATE=1   block inside the call until FIXTURE_DIR/release.gemini exists
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from notesagent.worker.gemini import GeminiReply

_SEGMENT = re.compile(r"^\[#(\d+) ", re.M)


def api_error(code: int, body: dict, cls_name: str | None = None):
    from google.genai import errors
    cls = getattr(errors, cls_name) if cls_name else (errors.ClientError if code < 500 else errors.ServerError)
    return cls(code, body)


def rate_limited_body(seconds: str) -> dict:
    return {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "rate limit",
                      "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{seconds}s"}]}}


QUOTA_BODY = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
                        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                     "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}


def chunk_answer(indices: list[int], title_hint: str = "") -> dict:
    first, last = indices[0], indices[-1]
    middle = indices[len(indices) // 2]
    return {
        "summary": f"Part covering segments {first}-{last}.{title_hint}",
        "key_points": [f"Key point from segment {first}", f"Key point from segment {last}"],
        "sections": [{"title": f"Section at {first}", "start_segment": first, "points": [f"Point {first}"]},
                     {"title": f"Section at {middle}", "start_segment": middle, "points": [f"Point {middle}"]}],
        "visual_moments": [{"segment": first, "caption": f"The speaker shows a diagram introduced in segment {first}.",
                            "reason": "The speaker says 'as you can see on this slide'."},
                           {"segment": last, "caption": f"The speaker presents a chart in segment {last}.",
                            "reason": "A chart is described."}],
    }


class FakeGemini:
    def __init__(self, fx: Path | None = None) -> None:
        self.fx = fx or Path(os.environ["FIXTURE_DIR"])
        self.scenario = os.environ.get("FAKE_GEMINI", "ok")

    def _mark(self, kind: str, info: str) -> int:
        (self.fx / f"call.gemini.{kind}.{uuid.uuid4().hex[:8]}").write_text(info)
        return len(list(self.fx.glob("call.gemini.*")))

    async def generate(self, *, system: str, prompt: str, schema: dict) -> GeminiReply:
        indices = [int(n) for n in _SEGMENT.findall(prompt)]
        kind = "chunk" if indices else "merge"
        n = self._mark(kind, json.dumps({"first": indices[0], "last": indices[-1]} if indices else {}))
        if os.environ.get("FAKE_GEMINI_GATE") == "1":
            (self.fx / f"started.gemini.{os.getpid()}").write_text("")
            while not (self.fx / "release.gemini").exists():
                time.sleep(0.05)
        s = self.scenario
        if s.startswith("rate_limited_once") and n == 1:
            raise api_error(429, rate_limited_body(s.split(":")[1]))
        if s == "quota":
            raise api_error(429, QUOTA_BODY)
        if s == "server_once" and n == 1:
            raise api_error(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}})
        if s == "network_once" and n == 1:
            import httpx2
            raise httpx2.ConnectError("connection refused")
        if s == "leaky_once" and n == 1:
            key = os.environ.get("GEMINI_API_KEY", "")
            raise api_error(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                            "message": f"API key not valid: {key} https://x?key={key}"}})
        if s == "blocked":
            return GeminiReply(text=None, prompt_tokens=100, output_tokens=0, finish_reason="SAFETY", blocked=True)
        if s == "invalid_json" or (s == "invalid_json_once" and n == 1):
            return GeminiReply(text="{not json", prompt_tokens=100, output_tokens=5, finish_reason="STOP", blocked=False)
        if s == "schema_mismatch":
            return GeminiReply(text=json.dumps({"summary": 5}), prompt_tokens=100, output_tokens=5,
                               finish_reason="STOP", blocked=False)
        if kind == "merge":
            body = {"summary": "Overall summary of the whole video.",
                    "key_points": ["Merged key point A", "Merged key point B"]}
        else:
            body = chunk_answer(indices)
            if s == "ungrounded":
                body["visual_moments"].append({"segment": 999_999, "caption": "x", "reason": "y"})
                body["sections"].append({"title": "ghost", "start_segment": -1, "points": []})
        prompt_tokens = max(1, len(prompt) // 4)
        return GeminiReply(text=json.dumps(body), prompt_tokens=prompt_tokens, output_tokens=200,
                           finish_reason="STOP", blocked=False)
