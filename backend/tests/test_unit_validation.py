"""URL / request validation without any network or database."""
import uuid

import pytest

from notesagent.common.errors import ApiError
from notesagent.gateway.validation import parse_submit_request, validate_idempotency_key
from notesagent.gateway.youtube import InvalidYouTubeUrl, extract_video_id

TOKEN = "ntn_" + "a" * 40


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "http://youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?t=10",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
    "  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ",
])
def test_valid_urls(url):
    assert extract_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    "", "not a url", "javascript:alert(1)", "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
    "https://vimeo.com/123456", "https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=short", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&v=aaaaaaaaaaa",
    "https://www.youtube.com/watch", "https://www.youtube.com/channel/UCxyz",
    "https://user:pw@www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/", "https://youtu.be/dQw4w9WgXcQ/extra", "https://www.youtube.com/watch?v=dQw4w9WgX!Q",
    "https://www.youtube.com/watch?v=" + "a" * 3000,
])
def test_invalid_urls(url):
    with pytest.raises(InvalidYouTubeUrl):
        extract_video_id(url)


def test_parse_valid_request_normalises_parent_id():
    parent = uuid.uuid4()
    req = parse_submit_request({"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "notion_token": TOKEN,
                                "notion_parent_id": parent.hex})
    assert req.video_id == "dQw4w9WgXcQ" and req.notion_parent_id == parent and req.notion_token == TOKEN


@pytest.mark.parametrize("payload,code", [
    ({"youtube_url": "https://youtu.be/dQw4w9WgXcQ"}, "INVALID_REQUEST"),
    ({"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "notion_token": TOKEN, "x": 1}, "INVALID_REQUEST"),
    ({"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "notion_token": TOKEN + " x"}, "INVALID_NOTION_TOKEN"),
    ({"youtube_url": "https://vimeo.com/1", "notion_token": TOKEN}, "INVALID_YOUTUBE_URL"),
    ({"youtube_url": "https://youtu.be/dQw4w9WgXcQ", "notion_token": TOKEN, "notion_parent_id": "zz"},
     "INVALID_NOTION_PARENT_ID"),
    (["not", "an", "object"], "INVALID_REQUEST"),
])
def test_invalid_requests_never_echo_token(payload, code):
    with pytest.raises(ApiError) as info:
        parse_submit_request(payload)
    assert info.value.code == code and info.value.status_code == 400
    assert TOKEN not in str(info.value.body())


@pytest.mark.parametrize("key,ok", [
    (None, True), ("3f2c8a6e-1b7d-4c55-9a43-0c9f5d2e7b11", True), ("short", False),
    ("has space in it", False), ("x" * 129, False), ("ok_key:v1.2", True),
])
def test_idempotency_key_format(key, ok):
    if ok:
        assert validate_idempotency_key(key) == key
    else:
        with pytest.raises(ApiError):
            validate_idempotency_key(key)
