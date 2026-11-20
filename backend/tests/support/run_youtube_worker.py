"""TEST-ONLY entrypoint: the real JobWorker with the PRODUCTION stage registry, but the external
clients replaced by fakes (no network): YouTube -> tests.support.fake_youtube.FakeYouTube,
Gemini -> tests.support.fake_gemini.FakeGemini. The fake YouTube "download" is a real synthetic video
made with FFmpeg, so EXTRACTING_FRAMES runs real FFmpeg extraction.

PUBLISHING is the production stage; it talks to NOTION_API_BASE_URL. Tests point that to the local
fake Notion server (tests.support.fake_notion). If unset, an unreachable local address is used, so a
test can never reach the real Notion API.

FAKE_YT_WITH_FIXTURES=1 replaces PUBLISHING with the fixture stand-in (no Notion at all) — used by the
earlier stage tests that only need a job to complete.
"""
import os

os.environ.setdefault("NOTION_API_BASE_URL", "http://127.0.0.1:9/v1")          # never the real Notion API

from notesagent.common.states import JobStatus  # noqa: E402
from notesagent.worker.__main__ import serve  # noqa: E402
from notesagent.worker.stages import build_stage_registry  # noqa: E402
from tests.support.fake_gemini import FakeGemini  # noqa: E402
from tests.support.fake_youtube import FakeYouTube  # noqa: E402
from tests.support.fixture_stages import build_fixture_stages  # noqa: E402

if __name__ == "__main__":
    stages = build_stage_registry(youtube=FakeYouTube(), gemini=FakeGemini())
    if os.environ.get("FAKE_YT_WITH_FIXTURES") == "1":
        stages[JobStatus.PUBLISHING] = build_fixture_stages()[JobStatus.PUBLISHING]
    serve(stages)
