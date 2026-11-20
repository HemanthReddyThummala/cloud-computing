"""YouTube access for the TRANSCRIBING stage (ADR-14): a thin client over ``yt-dlp`` (metadata,
true duration) and ``youtube-transcript-api`` (timestamped transcript), plus the mapping of their
exceptions to the classified pipeline errors (ADR-10).

The client methods are synchronous (both libraries use blocking HTTP); the stage runs them in a
thread. No API key is used. The libraries are imported lazily so other services do not load them.

Classification never stores or logs upstream text. For yt-dlp, the availability reasons are only
exposed as messages, so fixed patterns are matched against the message and only our own code and
message are kept.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from notesagent.common.errors import (
    FatalStageError,
    PipelineError,
    Provider,
    RateLimited,
    TransientError,
)
from notesagent.common.http_errors import classify_exception, classify_http

P = Provider.YOUTUBE


def _unavailable() -> FatalStageError:
    """Same code as the HTTP 404/410 classification (ADR-10 D3): permanent -> FAILED, never retried."""
    return FatalStageError("YOUTUBE_VIDEO_UNAVAILABLE", "The video is private, deleted or otherwise unavailable.",
                           provider=P)


class YouTubeClient:
    """Real client. Each method is ONE logical YouTube request (the stage takes a limiter permit
    before each call)."""

    def __init__(self, timeout_sec: float = 30.0) -> None:
        self.timeout_sec = timeout_sec

    def video_info(self, video_id: str) -> dict[str, Any]:
        import yt_dlp
        options = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                   "socket_timeout": self.timeout_sec, "retries": 0, "extractor_retries": 0}
        with yt_dlp.YoutubeDL(options) as ydl:
            # process=False: metadata only, no format selection or download.
            return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False, process=False)

    def transcript_list(self, video_id: str) -> Iterable[Any]:
        from youtube_transcript_api import YouTubeTranscriptApi
        return YouTubeTranscriptApi(http_client=_timeout_session(self.timeout_sec)).list(video_id)

    def fetch(self, transcript: Any) -> Any:
        return transcript.fetch()

    def download_video(self, video_id: str, workdir: str, max_height: int, max_bytes: int) -> dict[str, Any]:
        """EXTRACTING_FRAMES (ADR-16): download one video-only stream (≤ max_height, H.264 preferred)
        into ``workdir``. ONE logical request. Returns path, format, size and duration; never the
        stream URL (signed URLs are not logged or stored)."""
        import yt_dlp
        h = int(max_height)
        options = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
                   "socket_timeout": self.timeout_sec, "retries": 0, "fragment_retries": 0,
                   "extractor_retries": 0, "max_filesize": max_bytes, "overwrites": True,
                   "format": f"bv*[height<={h}][vcodec^=avc1]/bv*[height<={h}][ext=mp4]/bv*[height<={h}]/b[height<={h}]",
                   "outtmpl": f"{workdir}/source.%(ext)s"}
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)
        downloads = info.get("requested_downloads") or []
        path = downloads[0].get("filepath") if downloads else None
        return {"path": path, "format_id": info.get("format_id"), "ext": info.get("ext"),
                "width": info.get("width"), "height": info.get("height"), "vcodec": info.get("vcodec"),
                "duration": info.get("duration")}


def _timeout_session(timeout_sec: float):
    import requests

    class TimeoutSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", timeout_sec)
            return super().request(*args, **kwargs)

    return TimeoutSession()


# ===================================================================== classification
def _causes(exc: BaseException) -> list[BaseException]:
    """exc, the exceptions yt-dlp stores in ``exc_info``, and the __cause__/__context__ chain."""
    seen, out, stack = set(), [], [exc]
    while stack and len(out) < 8:
        item = stack.pop(0)
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        out.append(item)
        info = getattr(item, "exc_info", None)
        if isinstance(info, tuple) and len(info) > 1 and isinstance(info[1], BaseException):
            stack.append(info[1])
        stack.extend([item.__cause__, item.__context__])
    return out


def _plain_headers(headers: Any) -> dict[str, str] | None:
    """yt-dlp and urllib use ``email.message.Message`` (not a Mapping): copy to a plain dict."""
    items = getattr(headers, "items", None)
    if not callable(items):
        return None
    try:
        return {str(k): str(v) for k, v in items()}
    except (TypeError, ValueError):
        return None


def _http_status(item: BaseException) -> tuple[int, Any] | None:
    """(status, headers) of an HTTP error from yt-dlp's networking layer, urllib or requests."""
    response = getattr(item, "response", None)
    status = getattr(item, "status", None)
    if isinstance(status, int) and response is not None:                      # yt_dlp.networking HTTPError
        return status, _plain_headers(getattr(response, "headers", None))
    code = getattr(item, "code", None)
    if isinstance(code, int) and 100 <= code < 600:                            # urllib.error.HTTPError
        return code, _plain_headers(getattr(item, "headers", None))
    if response is not None and isinstance(getattr(response, "status_code", None), int):   # requests
        return response.status_code, _plain_headers(getattr(response, "headers", None))
    return None


# yt-dlp availability messages (matched case-insensitively; never stored)
_YTDLP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"not a bot|confirm you.re not a bot", re.I), "blocked"),
    (re.compile(r"confirm your age|age.restricted|inappropriate for some users", re.I), "age"),
    (re.compile(r"private video|video unavailable|this video is (no longer )?(un)?available|"
                r"has been removed|account associated with this video has been terminated|"
                r"members.only|join this channel|not made this video available in your country|"
                r"this live event will begin|premieres? in|is not a valid url|incomplete youtube id|"
                r"does not exist", re.I), "unavailable"),
    (re.compile(r"too many requests|http error 429", re.I), "rate_limited"),
]


def classify_metadata_error(exc: BaseException) -> PipelineError:
    """Map a ``yt-dlp`` exception (usually ``DownloadError``) to a classified error."""
    if isinstance(exc, PipelineError):
        return exc
    for item in _causes(exc):
        found = _http_status(item)
        if found:
            status, headers = found
            error = classify_http(P, status, headers)
            if error is not None:
                return error
    text = " ".join(str(item) for item in _causes(exc))
    for pattern, kind in _YTDLP_PATTERNS:
        if pattern.search(text):
            if kind == "blocked":
                return RateLimited("YOUTUBE_BLOCKED", "YouTube is temporarily refusing automated requests.",
                                   provider=P)
            if kind == "age":
                return FatalStageError("YOUTUBE_AGE_RESTRICTED", "Age-restricted videos are not supported.",
                                       provider=P)
            if kind == "unavailable":
                return _unavailable()
            return RateLimited("YOUTUBE_RATE_LIMITED", "YouTube is rate limiting requests.", provider=P)
    try:
        from yt_dlp.networking.exceptions import TransportError
    except ImportError:                                                      # pragma: no cover
        TransportError = ()  # type: ignore[assignment]
    if any(isinstance(item, TransportError) for item in _causes(exc)):
        return TransientError("YOUTUBE_UNREACHABLE", "YouTube could not be reached.", provider=P,
                              detail={"exception_type": type(exc).__name__})
    return classify_exception(P, exc)


def classify_transcript_error(exc: BaseException) -> PipelineError:
    """Map a ``youtube-transcript-api`` (or requests) exception to a classified error."""
    if isinstance(exc, PipelineError):
        return exc
    import youtube_transcript_api as yta

    if isinstance(exc, (yta.TranscriptsDisabled, yta.NoTranscriptFound)):
        return FatalStageError("YOUTUBE_TRANSCRIPT_UNAVAILABLE",
                               "This video has no transcript (captions are disabled or missing).", provider=P)
    if isinstance(exc, (yta.VideoUnavailable, yta.InvalidVideoId)):
        return _unavailable()
    if isinstance(exc, yta.AgeRestricted):
        return FatalStageError("YOUTUBE_AGE_RESTRICTED", "Age-restricted videos are not supported.", provider=P)
    if isinstance(exc, yta.VideoUnplayable):
        return FatalStageError("YOUTUBE_VIDEO_UNPLAYABLE", "The video cannot be played.", provider=P)
    if isinstance(exc, yta.RequestBlocked):                                  # includes IpBlocked
        return RateLimited("YOUTUBE_BLOCKED", "YouTube is temporarily refusing automated requests.", provider=P)
    for item in _causes(exc):                                                # YouTubeRequestFailed etc.
        found = _http_status(item)
        if found:
            error = classify_http(P, found[0], found[1])
            if error is not None:
                return error
    if isinstance(exc, yta.YouTubeTranscriptApiException):                   # PoToken, unparsable data …
        return TransientError("YOUTUBE_TRANSCRIPT_TEMPORARY", "The transcript could not be retrieved right now.",
                              provider=P, detail={"exception_type": type(exc).__name__})
    return classify_exception(P, exc)                                        # network errors -> transient
