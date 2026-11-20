"""ADR-17 PUBLISHING, unit level: the real NotionClient and stage, in-process against the fake Notion
app (httpx.ASGITransport) — no network, no infrastructure."""
import asyncio
import base64
import uuid

import httpx
import pytest

from notesagent.common.config import get_settings
from notesagent.common.errors import FatalStageError, Provider, RateLimited, TransientError
from notesagent.worker.notion import NotionClient, connection_key
from notesagent.worker.publish import FRAME_KEY, build_plan, frame_caption, make_publish_stage, rich
from notesagent.worker.stages import StageContext
from tests.support import fake_notion

TOKEN = "ntn_unit_test_token_never_logged_0123456789"
PARENT = "11111111-2222-3333-4444-555555555555"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"


@pytest.fixture(autouse=True)
def clean_fake(monkeypatch):
    monkeypatch.delenv("FAKE_NOTION", raising=False)
    fake_notion.reset()
    yield
    fake_notion.reset()


class FakeLimiter:
    def __init__(self):
        self.calls = []

    async def acquire(self, provider, **kw):
        self.calls.append((provider, kw.get("connection")))


def summary(sections=2, key_points=2, text="A short summary."):
    return {"summary": text, "key_points": [f"Point {i}" for i in range(key_points)],
            "sections": [{"title": f"Part {i}", "start_sec": 60.0 * i, "points": [f"Detail {i}a", f"Detail {i}b"]}
                         for i in range(sections)]}


def frames(n=2, requested=None):
    fr = [{"timestamp_sec": 10.0 * (i + 1), "segment_index": i, "caption": f"The speaker shows diagram {i}.",
           "reason": f"Segment {i} says 'look at this'.", "caption_basis": "transcript",
           "image": {"mime_type": "image/jpeg", "data_base64": base64.b64encode(JPEG).decode()}}
          for i in range(n)]
    return {"requested": n if requested is None else requested, "frames": fr, "failed": []}


def job(**over):
    return {"video_id": "jNQXAC9IVRw", "youtube_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "video_title": "Me at the zoo", "notion_parent_id": None, **over}


def run(*, job_row=None, summ=None, fr=None, limiter=None, job_id=None, token_reads=None, **settings):
    settings.setdefault("notion_default_parent_id", PARENT)
    s = get_settings(notion_api_base_url="http://fake/v1", **settings)
    stage = make_publish_stage(s, transport=httpx.ASGITransport(app=fake_notion.app))

    async def read_token():
        if token_reads is not None:
            token_reads.append(1)
        return TOKEN

    ctx = StageContext(job_id=job_id or uuid.uuid4(), job=job_row or job(),
                       checkpoints={"SUMMARIZING": summ or summary(), "EXTRACTING_FRAMES": fr or frames()},
                       worker_id="t", _credential_reader=read_token, limiter=limiter or FakeLimiter())
    return asyncio.run(stage(ctx))


def pages():
    return list(fake_notion.STATE["pages"].values())


def blocks_of(page):
    return fake_notion.STATE["children"][page["id"]]


def writes():
    return [r for r in fake_notion.STATE["requests"] if r["method"] in ("POST", "PATCH")]


# ===================================================================== plan
def test_plan_is_deterministic_with_marker_first():
    jid = uuid.uuid4()
    plan = build_plan(jid, job(), summary(), frames())
    assert plan == build_plan(jid, job(), summary(), frames())
    first = plan[0]["paragraph"]["rich_text"]
    assert f"job {jid}" in first[0]["text"]["content"] and first[1]["text"]["link"]["url"].startswith("https://www.youtube")
    kinds = [b.get("type", FRAME_KEY) for b in plan]
    assert kinds[:3] == ["paragraph", "heading_2", "paragraph"]
    assert kinds.count("heading_3") == 2 and kinds.count(FRAME_KEY) == 2
    h3 = [b for b in plan if b.get("type") == "heading_3"][1]["heading_3"]["rich_text"]
    assert h3[0]["text"]["content"] == "1:00" and h3[0]["text"]["link"]["url"].endswith("&t=60s")


def test_text_only_plan_has_no_frame_section():
    plan = build_plan(uuid.uuid4(), job(), summary(), {"requested": 0, "frames": []})
    assert not any(b.get("type") == "heading_2" and b["heading_2"]["rich_text"][0]["text"]["content"] == "Key frames"
                   for b in plan)


def test_missing_frames_are_explained():
    plan = build_plan(uuid.uuid4(), job(), summary(), frames(1, requested=3))
    assert "2 of 3 selected frames could not be captured" in plan[-1]["paragraph"]["rich_text"][0]["text"]["content"]


def test_long_text_is_split_into_2000_character_items():
    items = rich("x" * 4500)
    assert [len(i["text"]["content"]) for i in items] == [2000, 2000, 500]


def test_every_frame_caption_has_time_caption_and_reason():
    cap = frame_caption(frames()["frames"][0], "vid")
    text = "".join(i["text"]["content"] for i in cap)
    assert text.startswith("0:10 — The speaker shows diagram 0.") and "Why it was chosen" in text
    assert cap[0]["text"]["link"]["url"] == "https://www.youtube.com/watch?v=vid&t=10s"


# ===================================================================== happy path + permits + secrets
def test_publishes_one_page_with_captioned_real_frames():
    limiter = FakeLimiter()
    out = run(limiter=limiter)
    [page] = pages()
    assert out.result == {"notion_page_url": page["url"], "frames_requested": 2, "frames_published": 2}
    assert out.warnings == [] and page["parent"] == PARENT and page["title"] == "Me at the zoo"
    images = [b for b in blocks_of(page) if b["type"] == "image"]
    assert len(images) == 2
    for img in images:
        assert img["image"]["type"] == "file_upload" and img["image"]["caption"]
    assert len(limiter.calls) == len(fake_notion.STATE["requests"])                 # a permit per request
    assert {c for _, c in limiter.calls} == {connection_key(TOKEN)} and TOKEN not in connection_key(TOKEN)
    assert all(p is Provider.NOTION for p, _ in limiter.calls)


def test_text_only_page():
    out = run(fr={"requested": 0, "frames": [], "failed": []})
    assert out.result["frames_published"] == 0 and out.result["frames_requested"] == 0
    assert not [r for r in fake_notion.STATE["requests"] if "file_uploads" in r["path"]]


def test_job_parent_wins_over_default():
    other = "99999999-8888-7777-6666-555555555555"
    run(job_row=job(notion_parent_id=uuid.UUID(other)))
    assert pages()[0]["parent"] == other


def test_no_destination_fails_before_reading_the_token():
    reads = []
    with pytest.raises(FatalStageError, match="NOTION_PARENT_MISSING"):
        run(notion_default_parent_id=None, token_reads=reads)
    assert reads == [] and fake_notion.STATE["requests"] == []


def test_token_never_appears_in_errors_or_repr():
    with pytest.raises(FatalStageError) as err:
        import os
        os.environ["FAKE_NOTION"] = "unauthorized"
        try:
            run()
        finally:
            del os.environ["FAKE_NOTION"]
    assert err.value.code == "NOTION_UNAUTHORIZED"
    assert TOKEN not in str(err.value) and TOKEN not in repr(err.value)
    assert TOKEN not in repr(NotionClient(get_settings(), TOKEN, FakeLimiter()))


# ===================================================================== idempotency / reconciliation
def test_redelivery_reuses_the_page_and_writes_nothing():
    jid = uuid.uuid4()
    first = run(job_id=jid)
    before = len(writes())
    second = run(job_id=jid)
    assert len(pages()) == 1 and len(writes()) == before                   # no new page, upload or append
    assert second.result == first.result and second.checkpoint["resumed"] is True


def test_append_failure_then_resume_completes_without_duplicates(monkeypatch):
    jid = uuid.uuid4()
    monkeypatch.setenv("FAKE_NOTION", "fail_append_503_once")
    with pytest.raises(TransientError) as err:
        run(job_id=jid, notion_blocks_per_request=3)
    assert err.value.detail["notion_write_outcome_unknown"] is True
    monkeypatch.delenv("FAKE_NOTION")
    out = run(job_id=jid, notion_blocks_per_request=3)
    [page] = pages()
    plan_len = len(build_plan(jid, job(), summary(), frames()))
    assert len(blocks_of(page)) == plan_len and out.result["frames_published"] == 2
    markers = [b for b in blocks_of(page) if f"job {jid}" in str(b)]
    assert len(markers) == 1


def test_uncertain_create_is_reconciled_not_duplicated(monkeypatch):
    jid = uuid.uuid4()
    monkeypatch.setenv("FAKE_NOTION", "create_503_committed")
    with pytest.raises(TransientError) as err:
        run(job_id=jid)
    assert err.value.detail["notion_write_outcome_unknown"] is True and len(pages()) == 1   # it WAS saved
    monkeypatch.delenv("FAKE_NOTION")
    out = run(job_id=jid)
    assert len(pages()) == 1 and out.checkpoint["resumed"] is True and out.result["frames_published"] == 2


def test_same_title_from_another_job_is_never_reused():
    run(job_id=uuid.uuid4())
    run(job_id=uuid.uuid4())
    assert len(pages()) == 2


def test_batches_never_exceed_the_block_limit():
    run(notion_blocks_per_request=4, summ=summary(sections=6, key_points=10))
    creates = [r for r in fake_notion.STATE["requests"] if r["method"] == "PATCH"]
    assert creates and len(pages()) == 1                                     # create + several appends


# ===================================================================== errors and degradation
def test_rate_limit_is_classified_with_retry_after(monkeypatch):
    monkeypatch.setenv("FAKE_NOTION", "rate_limited_once")
    with pytest.raises(RateLimited) as err:
        run()
    assert err.value.code == "NOTION_RATE_LIMITED" and err.value.retry_after_sec == 1.0


def test_network_failure_is_transient_and_marks_writes_uncertain():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)
    client = NotionClient(get_settings(notion_api_base_url="http://fake/v1"), TOKEN, FakeLimiter(),
                          transport=httpx.MockTransport(boom))
    for method, unknown in (("GET", False), ("POST", True)):
        with pytest.raises(TransientError) as err:
            asyncio.run(client.request(method, "pages"))
        assert err.value.code == "NOTION_UNREACHABLE" and err.value.detail["notion_write_outcome_unknown"] is unknown


def test_permanent_upload_failure_omits_that_frame_with_a_warning(monkeypatch):
    monkeypatch.setenv("FAKE_NOTION", "upload_400_once")
    out = run()
    assert out.result["frames_published"] == 1 and out.result["frames_requested"] == 2
    assert out.warnings == [{"code": "FRAME_UPLOAD_FAILED", "timestamp_sec": 10.0, "reason": "NOTION_BAD_REQUEST"}]
    texts = [str(b) for b in blocks_of(pages()[0])]
    assert any("could not be uploaded to Notion" in t for t in texts)


def test_parent_id_setting_is_validated():
    assert get_settings(notion_default_parent_id="11111111222233334444555555555555").notion_default_parent_id == PARENT
    assert get_settings(notion_default_parent_id="").notion_default_parent_id is None
    with pytest.raises(SystemExit):
        get_settings(notion_default_parent_id="not-a-page-id")
