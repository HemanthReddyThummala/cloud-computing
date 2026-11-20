"""TEST-ONLY fake YouTube client for the TRANSCRIBING stage (no network).

It has the same three methods as ``notesagent.worker.youtube.YouTubeClient`` and raises the REAL
exception classes of yt-dlp / youtube-transcript-api / requests, so the production classification is
exercised. Each call writes ``FIXTURE_DIR/call.<kind>.<uuid>`` so tests can count calls across
processes.

Env:
  FAKE_YT            ok (default) | unavailable | http404 | http410 | private | age | bot
                     | disabled | no_transcript | empty_transcript | transcript_blocked
                     | rate_limited_once:<sec> | network_once | network_always | transcript_429_once:<sec>
  FAKE_YT_DURATION   duration returned by yt-dlp (default 600); "none" | "nan" | "-5" | "abc" for invalid
  FAKE_YT_GATE       info | transcript -> block in that call until FIXTURE_DIR/release.yt exists
  FAKE_YT_SEGMENTS   N -> a long transcript of N segments (one every 5 s) instead of the short one
  FAKE_YT_DOWNLOAD   ok (default) | rate_limited_once:<sec> | network_once | network_always | unavailable
                     | format_unavailable | unexpected | missing_file | corrupt
  FAKE_YT_VIDEO_SECONDS  length of the synthetic source video (default 300)
  (FAKE_YT_GATE=download blocks inside the download until FIXTURE_DIR/release.yt exists)
"""
from __future__ import annotations

import io
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

SNIPPETS = [("Welcome to the lecture", 0.0, 2.5), ("  Today: distributed queues  ", 2.5, 3.25),
            ("", 5.75, 1.0), ("RabbitMQ & quorum queues", 6.75, 4.0)]


@dataclass
class FakeSnippet:
    text: str
    start: float
    duration: float


@dataclass
class FakeFetched:
    snippets: list
    language: str = "English"
    language_code: str = "en"
    is_generated: bool = False


@dataclass
class FakeTranscript:
    language_code: str
    is_generated: bool
    language: str = "English"
    snippets: list = field(default_factory=list)

    def fetch(self):
        return FakeFetched([FakeSnippet(*s) for s in self.snippets], self.language, self.language_code,
                           self.is_generated)


def ytdlp_download_error(message: str, *, status: int | None = None, headers: dict | None = None,
                         transport: bool = False) -> Exception:
    """A yt-dlp DownloadError like the ones extract_info raises."""
    from yt_dlp.networking import Response
    from yt_dlp.networking.exceptions import HTTPError, TransportError
    from yt_dlp.utils import DownloadError, ExtractorError
    if status is not None:
        inner: Exception = HTTPError(Response(io.BytesIO(b""), "https://www.youtube.com/watch", headers or {},
                                              status=status))
    elif transport:
        inner = TransportError("Connection refused")
    else:
        inner = ExtractorError(message, expected=True)
    return DownloadError(f"ERROR: {message}", exc_info=(type(inner), inner, None))


def requests_http_error(status: int, headers: dict | None = None) -> Exception:
    """youtube-transcript-api's YouTubeRequestFailed, raised inside ``except HTTPError`` like the library."""
    import requests
    from youtube_transcript_api import YouTubeRequestFailed
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    try:
        try:
            raise requests.HTTPError(f"{status} error", response=response)
        except requests.HTTPError as error:
            raise YouTubeRequestFailed("vid", error)
    except YouTubeRequestFailed as exc:
        return exc


class FakeYouTube:
    def __init__(self, fx: Path | None = None) -> None:
        self.fx = fx or Path(os.environ["FIXTURE_DIR"])
        self.scenario = os.environ.get("FAKE_YT", "ok")

    def _mark(self, kind: str) -> int:
        (self.fx / f"call.{kind}.{uuid.uuid4().hex[:8]}").write_text(str(os.getpid()))
        return len(list(self.fx.glob(f"call.{kind}.*")))

    def _gate(self, where: str) -> None:
        if os.environ.get("FAKE_YT_GATE") == where:
            (self.fx / f"started.yt.{where}.{os.getpid()}").write_text("")
            while not (self.fx / "release.yt").exists():
                time.sleep(0.05)

    def _once(self, n: int) -> bool:
        return n == 1

    # ---------------------------------------------------------------- the three client methods
    def video_info(self, video_id: str) -> dict:
        n = self._mark("info")
        self._gate("info")
        s = self.scenario
        if s in ("unavailable", "private"):
            raise ytdlp_download_error("[youtube] x: Private video. Sign in if you've been granted access"
                                       if s == "private" else "[youtube] x: Video unavailable. This video has been removed")
        if s in ("http404", "http410"):
            raise ytdlp_download_error("HTTP Error", status=int(s[4:]))
        if s == "age":
            raise ytdlp_download_error("[youtube] x: Sign in to confirm your age")
        if s == "bot":
            raise ytdlp_download_error("[youtube] x: Sign in to confirm you’re not a bot")
        if s.startswith("rate_limited_once") and self._once(n):
            raise ytdlp_download_error("HTTP Error 429", status=429, headers={"Retry-After": s.split(":")[1]})
        if (s == "network_once" and self._once(n)) or s == "network_always":
            raise ytdlp_download_error("Unable to download webpage", transport=True)
        raw = os.environ.get("FAKE_YT_DURATION", "600")
        duration = {"none": None, "nan": float("nan"), "abc": "abc"}.get(raw, None if raw == "none" else raw)
        if isinstance(duration, str) and duration != "abc":
            duration = float(duration) if "." in duration else int(duration)
        return {"id": video_id, "title": "Distributed Systems Lecture", "duration": duration}

    def transcript_list(self, video_id: str):
        n = self._mark("list")
        self._gate("transcript")
        import youtube_transcript_api as yta
        s = self.scenario
        if s == "disabled":
            raise yta.TranscriptsDisabled(video_id)
        if s == "no_transcript":
            return []
        if s == "transcript_blocked":
            raise yta.IpBlocked(video_id)
        if s.startswith("transcript_429_once") and self._once(n):
            raise requests_http_error(429, {"Retry-After": s.split(":")[1]})
        snippets = [] if s == "empty_transcript" else SNIPPETS
        if os.environ.get("FAKE_YT_SEGMENTS"):                   # long transcript: one segment every 5 s
            count = int(os.environ["FAKE_YT_SEGMENTS"])
            snippets = [(f"Sentence number {i} explains part {i // 10} of the lecture in some detail.", i * 5.0, 4.5)
                        for i in range(count)]
        return [FakeTranscript("de", False, "Deutsch", snippets), FakeTranscript("en", True, "English (auto)", snippets),
                FakeTranscript("en", False, "English", snippets)]

    def fetch(self, transcript):
        self._mark("fetch")
        return transcript.fetch()

    def download_video(self, video_id: str, workdir: str, max_height: int, max_bytes: int) -> dict:
        """No network: a REAL synthetic video made by FFmpeg's test source (every frame differs)."""
        n = self._mark("download")
        (self.fx / f"workdir.{uuid.uuid4().hex[:8]}").write_text(workdir)      # for cleanup tests
        self._gate("download")
        s = os.environ.get("FAKE_YT_DOWNLOAD", "ok")
        if s.startswith("rate_limited_once") and n == 1:
            raise ytdlp_download_error("HTTP Error 429", status=429, headers={"Retry-After": s.split(":")[1]})
        if (s == "network_once" and n == 1) or s == "network_always":
            raise ytdlp_download_error("Unable to download video data", transport=True)
        if s == "unavailable":
            raise ytdlp_download_error("[youtube] x: Video unavailable. This video has been removed")
        if s == "format_unavailable":
            raise ytdlp_download_error("[youtube] x: Requested format is not available")
        if s == "unexpected":
            raise ytdlp_download_error("[youtube] x: some new unexpected extractor failure")
        if s == "missing_file":
            return {"path": None, "format_id": "x", "duration": 10}
        path = Path(workdir) / "source.mp4"
        if s == "corrupt":
            path.write_bytes(b"this is not a video file")
        else:
            make_test_video(path, float(os.environ.get("FAKE_YT_VIDEO_SECONDS", "300")))
        return {"path": str(path), "format_id": "fake", "ext": "mp4", "width": 320, "height": 240,
                "vcodec": "mpeg4", "duration": float(os.environ.get("FAKE_YT_VIDEO_SECONDS", "300"))}


def make_test_video(path: Path, seconds: float, size: str = "320x240", rate: int = 2) -> None:
    """A real, decodable video from FFmpeg's built-in test pattern (shows a running timestamp)."""
    import subprocess
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
                    "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}", "-c:v", "mpeg4", "-q:v", "5",
                    str(path)], check=True, capture_output=True, timeout=120)
