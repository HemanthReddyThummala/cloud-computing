"""Strict YouTube URL validation (no network access — the gateway does no pipeline work)."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_WATCH_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_SHORT_HOSTS = {"youtu.be", "www.youtu.be"}
_PATH_PREFIXES = {"shorts", "embed", "live", "v"}


class InvalidYouTubeUrl(ValueError):
    pass


def extract_video_id(url: str) -> str:
    """Return the 11-character video id, or raise InvalidYouTubeUrl."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise InvalidYouTubeUrl("URL is empty or too long")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise InvalidYouTubeUrl("URL is malformed") from exc
    if parts.scheme not in ("http", "https"):
        raise InvalidYouTubeUrl("URL must use http or https")
    if parts.username or parts.password or port not in (None, 80, 443):
        raise InvalidYouTubeUrl("URL must not contain credentials or a custom port")

    host = (parts.hostname or "").lower()
    candidate: str | None = None
    if host in _SHORT_HOSTS:
        segments = [s for s in parts.path.split("/") if s]
        candidate = segments[0] if len(segments) == 1 else None
    elif host in _WATCH_HOSTS:
        if parts.path.rstrip("/") == "/watch":
            values = parse_qs(parts.query).get("v", [])
            candidate = values[0] if len(values) == 1 else None
        else:
            segments = [s for s in parts.path.split("/") if s]
            if len(segments) == 2 and segments[0] in _PATH_PREFIXES:
                candidate = segments[1]
    else:
        raise InvalidYouTubeUrl("URL is not a YouTube video URL")

    if candidate is None or not _VIDEO_ID.fullmatch(candidate):
        raise InvalidYouTubeUrl("URL does not contain a valid YouTube video id")
    return candidate
