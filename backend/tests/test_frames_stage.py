"""ADR-16 EXTRACTING_FRAMES inside the REAL worker (production stages, real runner, outbox retries,
Redis limiter, REAL FFmpeg). YouTube/Gemini are fakes; the "downloaded" video is a synthetic video
generated with FFmpeg (no network). PUBLISHING is the fixture stand-in (not implemented yet)."""
import base64
import hashlib
from pathlib import Path

from tests.support.helpers import drain_outbox, job, submit_ok, wait_for_status, wait_until

WORKER = "tests.support.run_youtube_worker"
TERMINAL = {"COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"}
LONG = {"FAKE_YT_SEGMENTS": 40, "FAKE_YT_WITH_FIXTURES": "1"}        # fake Gemini picks 0.0 s and 195.0 s


def frames_cp(db, job_id):
    return [r["data"] for r in db.execute(
        "SELECT data FROM job_checkpoints WHERE job_id = %s AND stage = 'EXTRACTING_FRAMES'", (job_id,)).fetchall()]


def calls(fx, kind):
    return len(list(fx.glob(f"call.{kind}.*")))


def start(procs, fx, name="worker-a", **env):
    procs.start(name, WORKER, FIXTURE_DIR=fx, **env)
    procs.wait_for_log(name, "worker started")


def submit(api):
    job_id = submit_ok(api)
    drain_outbox()
    return job_id


def test_real_frames_are_extracted_at_the_selected_timestamps(api, db, procs, fx):
    job_id = submit(api)
    start(procs, fx, **LONG)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "COMPLETED" and row["warnings"] == []
    [cp] = frames_cp(db, job_id)
    summary = db.execute("SELECT data FROM job_checkpoints WHERE job_id = %s AND stage = 'SUMMARIZING'",
                         (job_id,)).fetchone()["data"]
    assert (cp["requested"], cp["extracted"]) == (2, 2)
    for frame, moment in zip(cp["frames"], summary["important_timestamps"]):
        assert frame["timestamp_sec"] == moment["timestamp_sec"] and frame["caption"] == moment["caption"]
        assert frame["caption_basis"] == "transcript"
        data = base64.b64decode(frame["image"]["data_base64"])
        assert data[:2] == b"\xff\xd8" and hashlib.sha256(data).hexdigest() == frame["image"]["sha256"]
    assert [f["timestamp_sec"] for f in cp["frames"]] == [0.0, 195.0]
    assert cp["frames"][0]["image"]["sha256"] != cp["frames"][1]["image"]["sha256"]
    assert calls(fx, "download") == 1
    assert all(not Path(p.read_text()).exists() for p in fx.glob("workdir.*"))   # temp files removed


def test_partial_frame_failure_completes_with_warnings(api, db, procs, fx):
    job_id = submit(api)
    start(procs, fx, FAKE_YT_VIDEO_SECONDS=100, **LONG)                         # 195 s is beyond the video
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "COMPLETED_WITH_WARNINGS"
    assert row["warnings"] == [{"code": "FRAME_EXTRACTION_FAILED", "timestamp_sec": 195.0,
                                "reason": "TIMESTAMP_OUT_OF_RANGE"}]
    [cp] = frames_cp(db, job_id)
    assert (cp["requested"], cp["extracted"], [f["timestamp_sec"] for f in cp["frames"]]) == (2, 1, [0.0])
    results = api.get(f"/jobs/{job_id}/results").json()
    assert results["status"] == "COMPLETED_WITH_WARNINGS" and results["warnings"][0]["timestamp_sec"] == 195.0


def test_all_frames_failing_continues_text_only(api, db, procs, fx):
    job_id = submit(api)
    start(procs, fx, FAKE_YT_DOWNLOAD="format_unavailable", **LONG)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "COMPLETED_WITH_WARNINGS" and row["attempts"] == 0
    assert [w["reason"] for w in row["warnings"]] == ["SOURCE_UNAVAILABLE", "SOURCE_UNAVAILABLE"]
    [cp] = frames_cp(db, job_id)
    assert cp["extracted"] == 0 and cp["frames"] == []
    assert db.execute("SELECT count(*) AS n FROM job_checkpoints WHERE job_id = %s AND stage = 'SUMMARIZING'",
                      (job_id,)).fetchone()["n"] == 1                           # text results retained


def test_download_429_is_retried_through_the_outbox(api, db, procs, fx):
    job_id = submit_ok(api)
    procs.start("publisher", "notesagent.outbox")
    start(procs, fx, FAKE_YT_DOWNLOAD="rate_limited_once:2", **LONG)
    retry = wait_until(lambda: job(db, job_id)["retry"], timeout=60, what="retry scheduled")
    assert retry["reason_code"] == "YOUTUBE_RATE_LIMITED" and retry["delay_sec"] >= 2
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert (row["status"], row["attempts"]) == ("COMPLETED", 1) and calls(fx, "download") == 2


def test_video_now_unavailable_fails_like_adr14(api, db, procs, fx):
    job_id = submit(api)
    start(procs, fx, FAKE_YT_DOWNLOAD="unavailable", **LONG)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert (row["status"], row["error"]["code"]) == ("FAILED", "YOUTUBE_VIDEO_UNAVAILABLE")


def test_redelivery_after_checkpoint_does_not_download_again(api, db, procs, fx):
    job_id = submit(api)
    procs.start("worker-a", WORKER, FIXTURE_DIR=fx, FIXTURE_GATE="PUBLISHING", **LONG)
    wait_until(lambda: list(fx.glob(f"started.{job_id}.PUBLISHING.*")), timeout=60, what="past EXTRACTING_FRAMES")
    procs.sigkill(procs.procs["worker-a"].pid)
    start(procs, fx, name="worker-b", **LONG)
    assert wait_for_status(db, job_id, TERMINAL, timeout=60)["status"] == "COMPLETED"
    assert calls(fx, "download") == 1 and len(frames_cp(db, job_id)) == 1


def test_crash_during_download_repeats_only_the_frames_and_cleans_up(api, db, procs, fx):
    job_id = submit(api)
    procs.start("worker-a", WORKER, FIXTURE_DIR=fx, FAKE_YT_GATE="download", **LONG)
    wait_until(lambda: list(fx.glob("started.yt.download.*")), timeout=60, what="inside the download")
    crashed_dir = Path(next(fx.glob("workdir.*")).read_text())
    assert crashed_dir.exists()
    procs.sigkill(procs.procs["worker-a"].pid)
    procs.procs["worker-a"].wait(timeout=10)                 # the crashed process is gone (reaped)
    start(procs, fx, name="worker-b", **LONG)
    row = wait_for_status(db, job_id, TERMINAL, timeout=60)
    assert row["status"] == "COMPLETED" and row["attempts"] == 0
    assert calls(fx, "download") == 2 and calls(fx, "info") == 1 and len(frames_cp(db, job_id)) == 1
    assert not crashed_dir.exists()                                              # leftover purged
