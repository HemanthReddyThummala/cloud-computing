"""EXTRACTING_FRAMES stage (ADR-16): genuine frames from the source video at the timestamps chosen by
SUMMARIZING (ADR-15).

1. Read ``important_timestamps`` from the SUMMARIZING checkpoint (no timestamps -> nothing to do,
   no YouTube request).
2. Take ONE ``Provider.YOUTUBE`` permit and download one video-only stream with ``yt-dlp`` into a
   private temporary directory.
3. For every timestamp: check it lies inside the real duration (``ffprobe``), let FFmpeg decode the
   frame at exactly that time from the downloaded file and encode it as JPEG (≤ FRAME_MAX_WIDTH), then
   verify the result with ``ffprobe`` (a decodable MJPEG image, non-empty, ≤ 5 MiB). Only bytes
   produced by FFmpeg from the source file are ever stored — nothing is generated or substituted.
4. Each frame keeps its timestamp, segment index and the Gemini caption/reason, marked
   ``caption_basis = "transcript"`` (the caption was written from the speech, not from the image).

Graceful degradation: a failed timestamp becomes a warning ``FRAME_EXTRACTION_FAILED`` (with the
timestamp) and the other frames continue. If the source cannot be obtained for a non-retryable
technical reason (format not available, download failed unexpectedly, file too large, FFmpeg
missing), ALL timestamps become such warnings and the stage still succeeds: PUBLISHING can publish a
text-only page, and the job ends COMPLETED_WITH_WARNINGS (runner rule). Retryable YouTube problems
(429, blocking, network, 5xx) are raised and retried by the existing outbox system; a now unavailable
or private video is FAILED (consistent with ADR-14).

The frames (base64 JPEG) are the stage checkpoint, stored with the transition in ONE transaction
before the ack; a redelivery after that does not download again. Temporary files are removed in every
case; leftovers of crashed workers are purged at the next stage start.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from notesagent.common.config import Settings
from notesagent.common.errors import FatalStageError, PipelineError, Provider, RateLimited
from notesagent.worker.stages import StageContext, StageOutput
from notesagent.worker.youtube import YouTubeClient, classify_metadata_error

log = logging.getLogger(__name__)

MAX_FRAME_BYTES = 5 * 1024 * 1024          # Notion free-workspace upload limit (5 MiB)
WARNING_CODE = "FRAME_EXTRACTION_FAILED"
_FORMAT_UNAVAILABLE = re.compile(r"requested format is not available|no video formats found|"
                                 r"format is not available|file is larger than max-filesize", re.I)
_RETRYABLE_CODES = {"YOUTUBE_RATE_LIMITED", "YOUTUBE_BLOCKED", "YOUTUBE_UNREACHABLE", "YOUTUBE_FORBIDDEN"}


class FrameError(Exception):
    """One timestamp could not be turned into a valid frame (never fatal for the job)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------- FFmpeg / ffprobe helpers
def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def probe_duration(path: str, timeout: float = 30) -> float | None:
    """Real duration of the downloaded file in seconds (None if unknown)."""
    try:
        r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path], timeout)
        value = float(json.loads(r.stdout or b"{}").get("format", {}).get("duration"))
        return value if math.isfinite(value) and value > 0 else None
    except (subprocess.TimeoutExpired, ValueError, TypeError, OSError):
        return None


def probe_image(path: str, timeout: float = 30) -> tuple[int, int]:
    """(width, height) if ``path`` is a decodable JPEG produced by FFmpeg; FrameError otherwise."""
    p = Path(path)
    if not p.exists():
        raise FrameError("NO_OUTPUT")
    size = p.stat().st_size
    if size == 0:
        raise FrameError("EMPTY_OUTPUT")
    if size > MAX_FRAME_BYTES:
        raise FrameError("TOO_LARGE")
    with p.open("rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            raise FrameError("NOT_A_JPEG")
    try:
        r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                  "stream=codec_name,width,height", "-of", "json", str(p)], timeout)
        streams = json.loads(r.stdout or b"{}").get("streams") or []
    except (subprocess.TimeoutExpired, ValueError, OSError):
        raise FrameError("UNREADABLE") from None
    if r.returncode != 0 or not streams or streams[0].get("codec_name") != "mjpeg":
        raise FrameError("UNREADABLE")
    width, height = int(streams[0].get("width") or 0), int(streams[0].get("height") or 0)
    if width <= 0 or height <= 0:
        raise FrameError("UNREADABLE")
    return width, height


def extract_frame(source: str, timestamp: float, out: str, *, max_width: int, quality: int,
                  timeout: float) -> tuple[int, int]:
    """Decode the frame at ``timestamp`` (accurate input seek) and encode it as JPEG."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{timestamp:.3f}",
           "-i", source, "-frames:v", "1", "-an", "-vf", f"scale='min({int(max_width)},iw)':-2",
           "-q:v", str(int(quality)), "-f", "image2", "-c:v", "mjpeg", out]
    try:
        r = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        raise FrameError("FFMPEG_TIMEOUT") from None
    except FileNotFoundError:
        raise FrameError("FFMPEG_MISSING") from None
    if r.returncode != 0:
        raise FrameError("FFMPEG_ERROR")
    return probe_image(out, timeout)


def valid_timestamp(value: Any, duration: float | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    if duration is not None and value >= duration:
        return None
    return float(value)


# ---------------------------------------------------------------- temporary files
def _process_alive(pid: int) -> bool:
    """True if ``pid`` is a running (non-zombie) process on this host/container."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                              # a killed but not yet reaped process is a zombie
        status = Path(f"/proc/{pid}/status").read_text()
        return "\nState:\tZ" not in status
    except OSError:
        return True


def purge_stale(root: Path, max_age_sec: float) -> int:
    """Remove work dirs of crashed workers: owner PID (dir prefix) not alive, or older than max_age."""
    removed = 0
    if not root.exists():
        return 0
    for entry in root.iterdir():
        try:
            pid = int(entry.name.split("-", 1)[0])
        except ValueError:
            pid = None
        # This process runs one job at a time, so its own leftovers are stale too.
        alive = pid is not None and pid != os.getpid() and _process_alive(pid)
        too_old = (time.time() - entry.stat().st_mtime) > max_age_sec
        if not alive or too_old:
            shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)
            removed += 1
    return removed


# ---------------------------------------------------------------- download error handling
def classify_download_error(exc: BaseException) -> PipelineError | None:
    """Raise-worthy classified error, or None = degrade (no frames, continue text-only)."""
    info = getattr(exc, "exc_info", None)
    inner = info[1] if isinstance(info, tuple) and len(info) > 1 else None
    text = " ".join(str(x) for x in (exc, inner) if x is not None)    # matched only, never stored
    if _FORMAT_UNAVAILABLE.search(text):
        return None
    error = classify_metadata_error(exc)
    if isinstance(error, FatalStageError):
        return error                                   # unavailable/private/age: as in ADR-14
    if isinstance(error, RateLimited) or error.code in _RETRYABLE_CODES or error.code.startswith("YOUTUBE_HTTP_5"):
        return error                                   # existing outbox retry + shared cooldown
    return None                                        # unexpected technical failure -> degrade


# ---------------------------------------------------------------- the stage
def make_frames_stage(settings: Settings, client: Any | None = None):
    youtube = client or YouTubeClient(timeout_sec=settings.youtube_request_timeout_sec)
    root = Path(settings.frame_work_dir)

    async def extract(ctx: StageContext) -> StageOutput:
        summary = ctx.checkpoints.get("SUMMARIZING")
        if summary is None:
            raise FatalStageError("SUMMARY_MISSING", "The summary of this job is missing.")
        moments = list(summary.get("important_timestamps") or [])
        base = {"requested": len(moments), "caption_basis": "transcript"}
        if not moments:
            return StageOutput(checkpoint={**base, "extracted": 0, "frames": [], "failed": [], "source": None})

        root.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(purge_stale, root, settings.frame_tmp_max_age_sec)
        workdir = tempfile.mkdtemp(prefix=f"{os.getpid()}-{ctx.job_id}-", dir=root)
        try:
            return await _extract_into(ctx, moments, base, workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)              # success, failure or cancellation

    async def _extract_into(ctx: StageContext, moments: list[dict[str, Any]], base: dict[str, Any],
                            workdir: str) -> StageOutput:
        video_id = ctx.job["video_id"]
        await ctx.limiter.acquire(Provider.YOUTUBE)                  # one logical YouTube request
        try:
            source = await asyncio.to_thread(youtube.download_video, video_id, workdir,
                                             settings.frame_source_max_height,
                                             settings.frame_download_max_mb * 1024 * 1024)
            source_problem = None if source.get("path") and Path(source["path"]).exists() else "SOURCE_MISSING"
        except Exception as exc:  # noqa: BLE001 - classified; upstream text (incl. signed URLs) never kept
            error = classify_download_error(exc)
            if error is not None:
                raise error from None
            source, source_problem = {}, "SOURCE_UNAVAILABLE"
        if source_problem:
            log.warning("source video not available for frames; continuing without frames",
                        extra={"job_id": str(ctx.job_id), "reason": source_problem})
            failed = [{"timestamp_sec": m.get("timestamp_sec"), "reason": source_problem} for m in moments]
            return _output(base, [], failed, None)

        duration = await asyncio.to_thread(probe_duration, source["path"]) or source.get("duration")
        frames, failed = [], []
        for index, moment in enumerate(moments):
            t = valid_timestamp(moment.get("timestamp_sec"), duration)
            if t is None:
                failed.append({"timestamp_sec": moment.get("timestamp_sec"), "reason": "TIMESTAMP_OUT_OF_RANGE"})
                continue
            out = str(Path(workdir) / f"frame-{index:02d}.jpg")
            try:
                width, height = await asyncio.to_thread(
                    extract_frame, source["path"], t, out, max_width=settings.frame_max_width,
                    quality=settings.frame_jpeg_quality, timeout=settings.frame_ffmpeg_timeout_sec)
                data = Path(out).read_bytes()
            except FrameError as exc:
                failed.append({"timestamp_sec": t, "reason": exc.reason})
                continue
            frames.append({
                "timestamp_sec": t, "segment_index": moment.get("segment_index"),
                "caption": moment.get("caption"), "reason": moment.get("reason"),
                "transcript_excerpt": moment.get("transcript_excerpt"), "caption_basis": "transcript",
                "image": {"mime_type": "image/jpeg", "width": width, "height": height, "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest(),
                          "data_base64": base64.b64encode(data).decode("ascii")}})
        info = {k: source.get(k) for k in ("format_id", "ext", "width", "height", "vcodec")}
        info["duration_sec"] = duration
        if failed:
            log.warning("some frames could not be extracted", extra={"job_id": str(ctx.job_id),
                                                                     "failed": len(failed), "extracted": len(frames)})
        return _output(base, frames, failed, info)

    return extract


def _output(base: dict[str, Any], frames: list[dict[str, Any]], failed: list[dict[str, Any]],
            source: dict[str, Any] | None) -> StageOutput:
    warnings = [{"code": WARNING_CODE, "timestamp_sec": f["timestamp_sec"], "reason": f["reason"]} for f in failed]
    checkpoint = {**base, "extracted": len(frames), "frames": frames, "failed": failed, "source": source}
    return StageOutput(checkpoint=checkpoint, warnings=warnings)
