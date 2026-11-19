"""TEST-ONLY stage implementations used to exercise the real worker orchestration.

They do NO pipeline work (no YouTube, no LLM, no frames, no Notion). They record each
execution in FIXTURE_DIR so tests can count how often a stage ran, optionally read the
Notion token through the real credential mailbox, optionally block ("gate") so a test can
SIGKILL the worker mid-stage, and optionally raise errors.

The PUBLISHING stand-in returns an obviously fake, clearly labelled URL so the worker's
result validation can be exercised. It is never a real Notion page.

Env:
  FIXTURE_DIR          directory for run/started/release/token files (required)
  FIXTURE_GATE         stage name at which to block until FIXTURE_DIR/release.<job_id> exists
  FIXTURE_TOKEN_STAGE  stage name at which to read the token via ctx.get_notion_token()
  FIXTURE_RAISE        stage name at which to raise an unexpected RuntimeError
  FIXTURE_RAISE_TIMES  only raise on the first N runs of that stage (default: every run)
  FIXTURE_RESULT       "valid" (default) | "invalid" (no URL) | "partial" (fewer frames published)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

from notesagent.common.states import PIPELINE_ORDER, JobStatus
from notesagent.worker.stages import Stage, StageContext, StageOutput

FAKE_URL = "https://www.notion.so/TEST-FIXTURE-not-a-real-page-00000000000000000000000000000000"


def _publish_result() -> dict | None:
    mode = os.environ.get("FIXTURE_RESULT", "valid")
    if mode == "invalid":
        return {"fixture": True}
    if mode == "partial":
        return {"notion_page_url": FAKE_URL, "frames_requested": 3, "frames_published": 2}
    return {"notion_page_url": FAKE_URL, "frames_requested": 0, "frames_published": 0}


def _make(status: JobStatus) -> Stage:
    async def stage(ctx: StageContext) -> StageOutput:
        fx = Path(os.environ["FIXTURE_DIR"])
        tag = f"{ctx.job_id}.{status.value}"
        (fx / f"run.{tag}.{ctx.worker_id}.{uuid.uuid4().hex[:8]}").write_text(str(os.getpid()))
        if status.value == os.environ.get("FIXTURE_RAISE"):
            times = os.environ.get("FIXTURE_RAISE_TIMES")         # unset = fail on every attempt
            if times is None or len(list(fx.glob(f"run.{tag}.*"))) <= int(times):
                raise RuntimeError("fixture: simulated unexpected error")
        if status.value == os.environ.get("FIXTURE_TOKEN_STAGE"):
            token = await ctx.get_notion_token()
            digest = hashlib.sha256(token.encode()).hexdigest()
            (fx / f"tokenhash.{ctx.job_id}.{uuid.uuid4().hex[:8]}").write_text(digest)
        if status.value == os.environ.get("FIXTURE_GATE"):
            (fx / f"started.{tag}.{os.getpid()}").write_text("")
            while not (fx / f"release.{ctx.job_id}").exists():
                await asyncio.sleep(0.05)
        result = _publish_result() if status is JobStatus.PUBLISHING else None
        return StageOutput(checkpoint={"stage": status.value, "worker": ctx.worker_id}, result=result)

    return stage


def build_fixture_stages() -> dict[JobStatus, Stage]:
    return {status: _make(status) for status in PIPELINE_ORDER}
