"""ADR-16 EXTRACTING_FRAMES, unit level: REAL FFmpeg/ffprobe on synthetic videos (no network, no
database). Download clients and the limiter are stubs."""
import asyncio
import base64
import hashlib
import math
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from notesagent.common.config import get_settings
from notesagent.common.errors import FatalStageError, Provider, RateLimited, RateLimiterUnavailable, TransientError
from notesagent.worker.frames import (
    FrameError,
    extract_frame,
    make_frames_stage,
    probe_duration,
    probe_image,
    purge_stale,
    valid_timestamp,
)
from notesagent.worker.stages import StageContext
from tests.support.fake_youtube import make_test_video, ytdlp_download_error


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp("src") / "source.mp4"
    make_test_video(path, 30, size="640x360")
    return path


class Recorder:
    def __init__(self):
        self.events = []


class FakeLimiter:
    def __init__(self, rec, error=None):
        self.rec, self.error = rec, error

    async def acquire(self, provider, **kw):
        self.rec.events.append(f"acquire:{provider.value}")
        if self.error:
            raise self.error


class StubDownloader:
    """Copies a prepared video into the stage's work dir, or raises a real yt-dlp error."""

    def __init__(self, rec, video=None, error=None, path_override=None):
        self.rec, self.video, self.error, self.path_override = rec, video, error, path_override
        self.workdirs = []

    def download_video(self, video_id, workdir, max_height, max_bytes):
        self.rec.events.append("download")
        self.workdirs.append(workdir)
        if self.error:
            raise self.error
        target = Path(workdir) / "source.mp4"
        target.write_bytes(Path(self.video).read_bytes())
        return {"path": self.path_override or str(target), "format_id": "fake", "ext": "mp4",
                "width": 640, "height": 360, "vcodec": "mpeg4", "duration": 30}


def moment(t, i=0):
    return {"timestamp_sec": t, "segment_index": i, "caption": f"The speaker shows item {i}.",
            "reason": f"Said in segment {i}.", "transcript_excerpt": f"excerpt {i}"}


def run(client, rec, moments, tmp_path, *, limiter_error=None, **settings):
    stage = make_frames_stage(get_settings(frame_work_dir=str(tmp_path / "work"), **settings), client)
    ctx = StageContext(job_id=uuid.uuid4(), job={"video_id": "v"},
                       checkpoints={"SUMMARIZING": {"important_timestamps": moments}},
                       worker_id="t", _credential_reader=None, limiter=FakeLimiter(rec, limiter_error))
    return asyncio.run(stage(ctx))


# ===================================================================== FFmpeg in the image
def test_worker_image_has_working_ffmpeg_and_ffprobe():
    for tool in ("ffmpeg", "ffprobe"):
        r = subprocess.run([tool, "-hide_banner", "-version"], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0 and r.stdout.startswith(f"{tool} version")


# ===================================================================== FFmpeg helpers
def test_extract_frame_produces_a_valid_scaled_jpeg(video, tmp_path):
    out = tmp_path / "f.jpg"
    assert extract_frame(str(video), 5.0, str(out), max_width=320, quality=3, timeout=60) == (320, 180)
    data = out.read_bytes()
    assert data[:2] == b"\xff\xd8" and 0 < len(data) < 5 * 1024 * 1024


def test_frames_are_never_upscaled(video, tmp_path):
    out = tmp_path / "f.jpg"
    assert extract_frame(str(video), 1.0, str(out), max_width=1280, quality=3, timeout=60) == (640, 360)


def test_frames_at_different_timestamps_are_different_real_frames(video, tmp_path):
    a, b, c = tmp_path / "a.jpg", tmp_path / "b.jpg", tmp_path / "c.jpg"
    for out, t in ((a, 2.0), (b, 20.0), (c, 2.0)):
        extract_frame(str(video), t, str(out), max_width=640, quality=3, timeout=60)
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()   # noqa: E731
    assert digest(a) != digest(b) and digest(a) == digest(c)          # same time -> same frame


def test_probe_duration(video):
    assert abs(probe_duration(str(video)) - 30) < 1
    assert probe_duration("/nonexistent.mp4") is None


@pytest.mark.parametrize("content,reason", [(None, "NO_OUTPUT"), (b"", "EMPTY_OUTPUT"),
                                            (b"garbage bytes", "NOT_A_JPEG"),
                                            (b"\xff\xd8 not really a jpeg", "UNREADABLE")])
def test_missing_empty_or_corrupt_output_is_rejected(tmp_path, content, reason):
    path = tmp_path / "x.jpg"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(FrameError) as err:
        probe_image(str(path))
    assert err.value.reason == reason


def test_ffmpeg_failure_on_a_corrupt_source(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(FrameError) as err:
        extract_frame(str(bad), 1.0, str(tmp_path / "o.jpg"), max_width=320, quality=3, timeout=60)
    assert err.value.reason == "FFMPEG_ERROR"


@pytest.mark.parametrize("value,expected", [(0, 0.0), (29.9, 29.9), (30, None), (31, None), (-1, None),
                                            (math.nan, None), (math.inf, None), ("5", None), (True, None),
                                            (None, None)])
def test_timestamp_validation_against_duration(value, expected):
    assert valid_timestamp(value, 30.0) == expected


# ===================================================================== the stage
def test_multiple_frames_keep_timestamp_and_caption_association(video, tmp_path):
    rec = Recorder()
    out = run(StubDownloader(rec, video), rec, [moment(1.0, 0), moment(10.5, 7), moment(25.0, 12)], tmp_path)
    cp = out.checkpoint
    assert (cp["requested"], cp["extracted"], cp["failed"], out.warnings) == (3, 3, [], [])
    assert [(f["timestamp_sec"], f["segment_index"]) for f in cp["frames"]] == [(1.0, 0), (10.5, 7), (25.0, 12)]
    for f, i in zip(cp["frames"], (0, 7, 12)):
        assert f["caption"] == f"The speaker shows item {i}." and f["reason"] == f"Said in segment {i}."
        assert f["caption_basis"] == "transcript" and f["transcript_excerpt"] == f"excerpt {i}"
        data = base64.b64decode(f["image"]["data_base64"])
        assert data[:2] == b"\xff\xd8" and hashlib.sha256(data).hexdigest() == f["image"]["sha256"]
        assert (f["image"]["mime_type"], f["image"]["width"], f["image"]["height"]) == ("image/jpeg", 640, 360)
    assert len({f["image"]["sha256"] for f in cp["frames"]}) == 3                 # three distinct real frames
    assert cp["source"]["format_id"] == "fake" and abs(cp["source"]["duration_sec"] - 30) < 1


def test_partial_failures_keep_the_good_frames(video, tmp_path):
    rec = Recorder()
    moments = [moment(2.0, 0), moment(45.0, 1), moment(-3, 2), moment(float("nan"), 3), moment(12.0, 4)]
    out = run(StubDownloader(rec, video), rec, moments, tmp_path)
    cp = out.checkpoint
    assert [f["timestamp_sec"] for f in cp["frames"]] == [2.0, 12.0] and cp["extracted"] == 2
    assert [w["code"] for w in out.warnings] == ["FRAME_EXTRACTION_FAILED"] * 3
    assert out.warnings[0] == {"code": "FRAME_EXTRACTION_FAILED", "timestamp_sec": 45.0,
                               "reason": "TIMESTAMP_OUT_OF_RANGE"}


def test_all_frames_failing_still_succeeds_text_only(tmp_path):
    rec = Recorder()
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"not a video")
    out = run(StubDownloader(rec, corrupt), rec, [moment(1.0), moment(2.0, 1)], tmp_path)
    assert out.checkpoint["extracted"] == 0 and out.checkpoint["frames"] == []
    assert len(out.warnings) == 2 and {w["reason"] for w in out.warnings} == {"FFMPEG_ERROR"}


@pytest.mark.parametrize("error,reason", [
    (ytdlp_download_error("[youtube] x: Requested format is not available"), "SOURCE_UNAVAILABLE"),
    (ytdlp_download_error("[youtube] x: some new unexpected extractor failure"), "SOURCE_UNAVAILABLE"),
])
def test_non_retryable_source_problems_degrade_to_text_only(tmp_path, error, reason):
    rec = Recorder()
    out = run(StubDownloader(rec, error=error), rec, [moment(1.0), moment(5.0, 1)], tmp_path)
    assert out.checkpoint["extracted"] == 0 and out.checkpoint["source"] is None
    assert [w["reason"] for w in out.warnings] == [reason, reason]


def test_missing_downloaded_file_degrades(video, tmp_path):
    rec = Recorder()
    out = run(StubDownloader(rec, video, path_override="/nonexistent/source.mp4"), rec, [moment(1.0)], tmp_path)
    assert out.checkpoint["extracted"] == 0 and out.warnings[0]["reason"] == "SOURCE_MISSING"


@pytest.mark.parametrize("error,cls,code", [
    (ytdlp_download_error("HTTP Error 429", status=429, headers={"Retry-After": "5"}), RateLimited, "YOUTUBE_RATE_LIMITED"),
    (ytdlp_download_error("Unable to download", transport=True), TransientError, "YOUTUBE_UNREACHABLE"),
    (ytdlp_download_error("HTTP Error 503", status=503), TransientError, "YOUTUBE_HTTP_503"),
    (ytdlp_download_error("[youtube] x: Sign in to confirm you’re not a bot"), RateLimited, "YOUTUBE_BLOCKED"),
    (ytdlp_download_error("[youtube] x: Video unavailable. This video has been removed"), FatalStageError,
     "YOUTUBE_VIDEO_UNAVAILABLE"),
    (ytdlp_download_error("HTTP Error 404", status=404), FatalStageError, "YOUTUBE_VIDEO_UNAVAILABLE"),
])
def test_retryable_and_permanent_download_errors_are_raised(tmp_path, error, cls, code):
    rec = Recorder()
    with pytest.raises(cls) as err:
        run(StubDownloader(rec, error=error), rec, [moment(1.0)], tmp_path)
    assert err.value.code == code and err.value.__suppress_context__


def test_limiter_permit_before_the_download(video, tmp_path):
    rec = Recorder()
    run(StubDownloader(rec, video), rec, [moment(1.0)], tmp_path)
    assert rec.events == ["acquire:YOUTUBE", "download"]


def test_no_permit_no_download(video, tmp_path):
    rec = Recorder()
    with pytest.raises(RateLimiterUnavailable):
        run(StubDownloader(rec, video), rec, [moment(1.0)], tmp_path,
            limiter_error=RateLimiterUnavailable("RATE_LIMITER_UNAVAILABLE", "x"))
    assert rec.events == ["acquire:YOUTUBE"]


def test_no_timestamps_means_no_youtube_request(tmp_path):
    rec = Recorder()
    out = run(StubDownloader(rec), rec, [], tmp_path)
    assert rec.events == [] and out.checkpoint == {"requested": 0, "caption_basis": "transcript", "extracted": 0,
                                                   "frames": [], "failed": [], "source": None}


def test_summary_missing_is_fatal(tmp_path):
    stage = make_frames_stage(get_settings(frame_work_dir=str(tmp_path / "w")), StubDownloader(Recorder()))
    ctx = StageContext(job_id=uuid.uuid4(), job={"video_id": "v"}, checkpoints={}, worker_id="t",
                       _credential_reader=None, limiter=FakeLimiter(Recorder()))
    with pytest.raises(FatalStageError, match="SUMMARY_MISSING"):
        asyncio.run(stage(ctx))


# ===================================================================== temporary files
@pytest.mark.parametrize("case", ["success", "raised", "degraded"])
def test_temporary_files_are_removed_in_every_case(video, tmp_path, case):
    rec = Recorder()
    error = {"raised": ytdlp_download_error("HTTP Error 429", status=429),
             "degraded": ytdlp_download_error("Requested format is not available")}.get(case)
    client = StubDownloader(rec, video, error=error)
    if case == "raised":
        with pytest.raises(RateLimited):
            run(client, rec, [moment(1.0)], tmp_path)
    else:
        run(client, rec, [moment(1.0)], tmp_path)
    assert client.workdirs and not Path(client.workdirs[0]).exists()
    assert list((tmp_path / "work").iterdir()) == []


def test_purge_removes_leftovers_of_dead_or_old_workers(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    dead = root / "999999-job-a"                      # PID that does not exist
    own = root / f"{os.getpid()}-job-b"                # this process runs one job at a time
    live = root / f"{os.getppid()}-job-c"              # another live process: kept
    for d in (dead, own, live):
        d.mkdir()
        (d / "source.mp4").write_bytes(b"x" * 1000)
    assert purge_stale(root, max_age_sec=3600) == 2
    assert [p.name for p in root.iterdir()] == [live.name]
    old = live.stat().st_mtime - 7200
    os.utime(live, (old, old))
    assert purge_stale(root, max_age_sec=3600) == 1 and list(root.iterdir()) == []


def test_zombie_owner_counts_as_dead(tmp_path):
    """A SIGKILLed but not yet reaped worker is a zombie: its work dir must still be purged."""
    import signal
    from notesagent.worker.frames import _process_alive
    child = subprocess.Popen(["sleep", "60"])
    try:
        assert _process_alive(child.pid)
        os.kill(child.pid, signal.SIGKILL)
        import time
        time.sleep(0.3)                                          # dead, not reaped -> zombie
        assert not _process_alive(child.pid)
        root = tmp_path / "work"
        (root / f"{child.pid}-job").mkdir(parents=True)
        assert purge_stale(root, max_age_sec=3600) == 1
    finally:
        child.wait(timeout=5)


def test_frame_settings_defaults():
    s = get_settings()
    assert (s.frame_source_max_height, s.frame_max_width, s.frame_jpeg_quality) == (720, 1280, 3)
    assert s.frame_work_dir == "/tmp/notesagent-frames"
