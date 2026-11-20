"""ADR-17 PUBLISHING inside the REAL worker (all four production stages, real runner, outbox retries,
Redis limiter, real FFmpeg), with YouTube/Gemini faked and Notion replaced by the local fake Notion HTTP
server (tests.support.fake_notion, separate process). No network."""
import hashlib
import time

import httpx

from tests.support.helpers import drain_outbox, job, new_token, submit, submit_ok, wait_for_status, wait_until

WORKER = "tests.support.run_youtube_worker"
TERMINAL = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"}
PORT = 18765
NOTION = f"http://127.0.0.1:{PORT}"
PARENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ENV = {"FAKE_YT_SEGMENTS": 40, "NOTION_API_BASE_URL": f"{NOTION}/v1", "NOTION_DEFAULT_PARENT_ID": PARENT}


def start_notion(procs, fx, flags=""):
    procs.start("notion", "tests.support.fake_notion", PORT, FAKE_NOTION=flags, FAKE_NOTION_DIR=fx)
    wait_until(lambda: _alive(), timeout=30, what="fake Notion server up")


def _alive():
    try:
        return httpx.get(f"{NOTION}/_state", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def state():
    return httpx.get(f"{NOTION}/_state", timeout=5).json()


def start_worker(procs, fx, name="worker-a", **env):
    procs.start(name, WORKER, FIXTURE_DIR=fx, **{**ENV, **env})
    procs.wait_for_log(name, "worker started")


def writes(st):
    return [r for r in st["requests"] if r["method"] in ("POST", "PATCH")]


def publish_cp(db, job_id):
    return db.execute("SELECT data FROM job_checkpoints WHERE job_id = %s AND stage = 'PUBLISHING'",
                      (job_id,)).fetchall()


# ===================================================================== success / degradation
def test_full_pipeline_publishes_one_page_with_real_frames(api, db, procs, fx):
    start_notion(procs, fx)
    token = new_token()
    job_id = submit_ok(api, token=token)
    drain_outbox()
    start_worker(procs, fx)
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert row["status"] == "COMPLETED", row["error"]
    st = state()
    [page] = st["pages"]
    assert page["parent"] == PARENT and page["title"] == "Distributed Systems Lecture"
    assert row["result"] == {"notion_page_url": page["url"], "frames_requested": 2, "frames_published": 2}
    images = [b for b in page["children"] if b["type"] == "image"]
    assert len(images) == 2 and all(i["image"]["caption"] for i in images)
    assert f"job {job_id}" in page["children"][0]["paragraph"]["rich_text"][0]["plain_text"]
    assert st["tokens"] == [hashlib.sha256(token.encode()).hexdigest()]       # mailbox token used, by hash
    results = api.get(f"/jobs/{job_id}/results").json()
    assert results["result"]["notion_page_url"].startswith("https://www.notion.so/")
    assert token not in procs.all_logs()                                      # never logged


def test_text_only_page_when_no_frame_could_be_captured(api, db, procs, fx):
    start_notion(procs, fx)
    job_id = submit_ok(api)
    drain_outbox()
    start_worker(procs, fx, FAKE_YT_DOWNLOAD="format_unavailable")
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert row["status"] == "COMPLETED_WITH_WARNINGS"
    assert (row["result"]["frames_requested"], row["result"]["frames_published"]) == (2, 0)
    [page] = state()["pages"]
    assert not [b for b in page["children"] if b["type"] == "image"]


def test_partial_frames_complete_with_warnings(api, db, procs, fx):
    start_notion(procs, fx)
    job_id = submit_ok(api)
    drain_outbox()
    start_worker(procs, fx, FAKE_YT_VIDEO_SECONDS=100)
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert row["status"] == "COMPLETED_WITH_WARNINGS"
    assert (row["result"]["frames_requested"], row["result"]["frames_published"]) == (2, 1)
    assert row["warnings"][0]["code"] == "FRAME_EXTRACTION_FAILED"


def test_job_specific_parent_is_used(api, db, procs, fx):
    start_notion(procs, fx)
    own = "12345678-1234-1234-1234-123456789abc"
    r = submit(api, parent=own)
    job_id = r.json()["job_id"]
    drain_outbox()
    start_worker(procs, fx)
    assert wait_for_status(db, job_id, TERMINAL, timeout=90)["status"] == "COMPLETED"
    assert [p["parent"] for p in state()["pages"]] == [own]


# ===================================================================== errors -> existing retries
def test_notion_429_is_retried_and_publishes_once(api, db, procs, fx):
    start_notion(procs, fx, "rate_limited_once")
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start_worker(procs, fx)
    retry = wait_until(lambda: job(db, job_id)["retry"], timeout=90, what="retry scheduled")
    assert retry["reason_code"] == "NOTION_RATE_LIMITED"
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert (row["status"], row["attempts"]) == ("COMPLETED", 1) and len(state()["pages"]) == 1


def test_uncertain_page_creation_is_reconciled_not_duplicated(api, db, procs, fx):
    start_notion(procs, fx, "create_503_committed")
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start_worker(procs, fx)
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert (row["status"], row["attempts"]) == ("COMPLETED", 1)
    st = state()
    assert len(st["pages"]) == 1 and len([r for r in st["requests"] if r["path"] == "/v1/pages"]) == 1
    [cp] = publish_cp(db, job_id)
    assert cp["data"]["resumed"] is True


def test_unauthorized_token_fails(api, db, procs, fx):
    start_notion(procs, fx, "unauthorized")
    job_id = submit_ok(api)
    drain_outbox()
    start_worker(procs, fx)
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert (row["status"], row["error"]["code"]) == ("FAILED", "NOTION_UNAUTHORIZED") and state()["pages"] == []


# ===================================================================== crashes
def test_crash_mid_append_resumes_without_a_second_page(api, db, procs, fx):
    start_notion(procs, fx, "gate_append")
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("worker-a", WORKER, FIXTURE_DIR=fx, NOTION_BLOCKS_PER_REQUEST=3, **ENV)
    wait_until(lambda: list(fx.glob("started.notion.append")), timeout=90, what="inside the 2nd append")
    procs.sigkill(procs.procs["worker-a"].pid)
    procs.procs["worker-a"].wait(timeout=10)
    (fx / "release.notion").write_text("")          # the in-flight append completes on the server
    start_worker(procs, fx, name="worker-b", NOTION_BLOCKS_PER_REQUEST=3)
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    assert row["status"] == "COMPLETED" and row["attempts"] == 0
    st = state()
    [page] = st["pages"]
    [cp] = publish_cp(db, job_id)
    assert cp["data"]["resumed"] is True and cp["data"]["blocks"] == len(page["children"])
    assert sum(f"job {job_id}" in str(b) for b in page["children"]) == 1          # marker once: no duplicates
    assert len([b for b in page["children"] if b["type"] == "image"]) == 2


def test_crash_after_all_writes_before_commit_writes_nothing_more(api, db, procs, fx):
    start_notion(procs, fx, "gate_final_list")
    job_id = submit_ok(api)
    drain_outbox()
    procs.start("worker-a", WORKER, FIXTURE_DIR=fx, **ENV)
    wait_until(lambda: list(fx.glob("started.notion.final_list")), timeout=90, what="page fully written")
    before = len(writes(state()))
    procs.sigkill(procs.procs["worker-a"].pid)
    procs.procs["worker-a"].wait(timeout=10)
    (fx / "release.notion").write_text("")
    start_worker(procs, fx, name="worker-b")
    row = wait_for_status(db, job_id, TERMINAL, timeout=90)
    time.sleep(0.5)
    st = state()
    assert row["status"] == "COMPLETED" and len(st["pages"]) == 1
    assert len(writes(st)) == before                                          # redelivery wrote nothing
    assert row["result"]["notion_page_url"] == st["pages"][0]["url"]
