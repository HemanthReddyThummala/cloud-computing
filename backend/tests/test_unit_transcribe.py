"""ADR-14 TRANSCRIBING stage, unit level (no network, no infrastructure): duration rule, transcript
shape, limiter ordering and the classification of the REAL library exceptions."""
import asyncio
import uuid

import pytest
import requests

from notesagent.common.config import get_settings
from notesagent.common.errors import (
    FatalStageError,
    Provider,
    RateLimited,
    RateLimiterUnavailable,
    TransientError,
)
from notesagent.worker.stages import StageContext
from notesagent.worker.transcribe import check_duration, make_transcribe_stage, select_transcript, to_segments
from notesagent.worker.youtube import classify_metadata_error, classify_transcript_error
from tests.support.fake_youtube import (
    FakeSnippet,
    FakeTranscript,
    requests_http_error,
    ytdlp_download_error,
)


class Recorder:
    def __init__(self):
        self.events = []


class FakeLimiter:
    def __init__(self, rec, error=None):
        self.rec, self.error = rec, error

    async def acquire(self, provider, **kwargs):
        self.rec.events.append(f"acquire:{provider.value}")
        if self.error:
            raise self.error


class StubClient:
    """In-process fake; records every call next to the limiter's events."""

    def __init__(self, rec, *, info=None, info_error=None, transcripts=None, list_error=None, fetch_error=None):
        self.rec, self.info, self.info_error = rec, info, info_error
        self.transcripts, self.list_error, self.fetch_error = transcripts, list_error, fetch_error

    def video_info(self, video_id):
        self.rec.events.append("video_info")
        if self.info_error:
            raise self.info_error
        return self.info if self.info is not None else {"title": "Lecture", "duration": 600}

    def transcript_list(self, video_id):
        self.rec.events.append("transcript_list")
        if self.list_error:
            raise self.list_error
        return self.transcripts if self.transcripts is not None else [
            FakeTranscript("en", False, "English", [("Hello", 0.0, 1.5), ("World", 1.5, 2.0)])]

    def fetch(self, transcript):
        self.rec.events.append(f"fetch:{transcript.language_code}:{transcript.is_generated}")
        if self.fetch_error:
            raise self.fetch_error
        return transcript.fetch()


def run_stage(client, rec, *, limiter_error=None, **settings):
    stage = make_transcribe_stage(get_settings(**settings), client)
    ctx = StageContext(job_id=uuid.uuid4(), job={"video_id": "dQw4w9WgXcQ"}, checkpoints={}, worker_id="t",
                       _credential_reader=None, limiter=FakeLimiter(rec, limiter_error))
    return asyncio.run(stage(ctx))


# ===================================================================== success and data shape
def test_success_extracts_title_duration_and_timestamped_segments():
    rec = Recorder()
    transcripts = [FakeTranscript("en", False, "English",
                                  [("  Intro  ", 0.0, 2.5), ("", 2.5, 1.0), ("Queues", 3.5, 4.25)])]
    out = run_stage(StubClient(rec, info={"title": " Lecture 1 ", "duration": 754}, transcripts=transcripts), rec)
    assert out.job_fields == {"video_title": "Lecture 1", "duration_sec": 754}
    t = out.checkpoint["transcript"]
    assert (t["source"], t["language_code"], t["is_generated"], t["segment_count"]) == \
           ("youtube-transcript-api", "en", False, 2)
    assert t["segments"] == [{"start": 0.0, "duration": 2.5, "end": 2.5, "text": "Intro"},
                             {"start": 3.5, "duration": 4.25, "end": 7.75, "text": "Queues"}]   # empty dropped
    assert out.checkpoint["duration_sec"] == 754 and out.checkpoint["video_id"] == "dQw4w9WgXcQ"
    assert out.result is None and out.warnings == []


def test_every_youtube_request_takes_a_limiter_permit_first():
    rec = Recorder()
    run_stage(StubClient(rec), rec)
    assert rec.events == ["acquire:YOUTUBE", "video_info", "acquire:YOUTUBE", "transcript_list",
                          "acquire:YOUTUBE", "fetch:en:False"]


@pytest.mark.parametrize("error", [RateLimiterUnavailable("RATE_LIMITER_UNAVAILABLE", "x"),
                                   RateLimited("YOUTUBE_COOLDOWN_ACTIVE", "x", provider=Provider.YOUTUBE,
                                               retry_after_sec=5)])
def test_no_request_without_a_permit(error):
    rec = Recorder()
    with pytest.raises(type(error)) as exc:
        run_stage(StubClient(rec), rec, limiter_error=error)
    assert exc.value is error and rec.events == ["acquire:YOUTUBE"]          # provider never called


def test_transcript_language_preference():
    both = [FakeTranscript("de", False), FakeTranscript("en", True), FakeTranscript("en", False)]
    assert (select_transcript(both, ["en"]).language_code, select_transcript(both, ["en"]).is_generated) == ("en", False)
    assert select_transcript([FakeTranscript("en", True), FakeTranscript("de", False)], ["en"]).is_generated is True
    assert select_transcript([FakeTranscript("de", True), FakeTranscript("fr", False)], ["en"]).language_code == "fr"
    assert select_transcript([FakeTranscript("de", False), FakeTranscript("en", False)], ["de", "en"]).language_code == "de"
    with pytest.raises(FatalStageError) as err:
        select_transcript([], ["en"])
    assert err.value.code == "YOUTUBE_TRANSCRIPT_UNAVAILABLE"


def test_segments_keep_order_text_and_timestamps():
    segs = to_segments([FakeSnippet("a", 1.23456, 2.0), FakeSnippet("b\nc", 3.2, 0.5)])
    assert segs == [{"start": 1.235, "duration": 2.0, "end": 3.235, "text": "a"},
                    {"start": 3.2, "duration": 0.5, "end": 3.7, "text": "b\nc"}]


# ===================================================================== constraint A
@pytest.mark.parametrize("duration", [1200, 1200.0, 1, 599.5])
def test_up_to_1200_seconds_is_accepted(duration):
    assert check_duration(duration, 1200) == float(duration)


def test_exactly_1200_passes_the_whole_stage():
    rec = Recorder()
    out = run_stage(StubClient(rec, info={"title": "t", "duration": 1200}), rec)
    assert out.job_fields["duration_sec"] == 1200


@pytest.mark.parametrize("duration", [1201, 1200.5, 7200])
def test_longer_than_1200_is_rejected_before_any_transcript_request(duration):
    rec = Recorder()
    with pytest.raises(FatalStageError) as err:
        run_stage(StubClient(rec, info={"title": "t", "duration": duration}), rec)
    assert err.value.code == "VIDEO_TOO_LONG" and "20:00" in err.value.message
    assert rec.events == ["acquire:YOUTUBE", "video_info"]                  # no transcript request


@pytest.mark.parametrize("duration", [None, 0, -5, float("nan"), float("inf"), "600", True, [600]])
def test_missing_or_invalid_duration_fails_and_is_never_zero(duration):
    rec = Recorder()
    with pytest.raises(FatalStageError) as err:
        run_stage(StubClient(rec, info={"title": "t", "duration": duration}), rec)
    assert err.value.code == "VIDEO_DURATION_UNKNOWN"
    assert rec.events == ["acquire:YOUTUBE", "video_info"]


def test_missing_duration_key():
    rec = Recorder()
    with pytest.raises(FatalStageError, match="VIDEO_DURATION_UNKNOWN"):
        run_stage(StubClient(rec, info={"title": "t"}), rec)


def test_max_duration_is_configurable():
    rec = Recorder()
    with pytest.raises(FatalStageError) as err:
        run_stage(StubClient(rec, info={"title": "t", "duration": 301}), rec, max_video_duration_sec=300)
    assert err.value.code == "VIDEO_TOO_LONG" and "5:00" in err.value.message


# ===================================================================== transcript failures
def test_transcripts_disabled_or_missing_is_failed():
    import youtube_transcript_api as yta
    for error in (yta.TranscriptsDisabled("v"), yta.NoTranscriptFound("v", ["en"], None)):
        rec = Recorder()
        with pytest.raises(FatalStageError) as err:
            run_stage(StubClient(rec, list_error=error), rec)
        assert err.value.code == "YOUTUBE_TRANSCRIPT_UNAVAILABLE" and err.value.provider is Provider.YOUTUBE


def test_no_transcript_in_list_or_empty_transcript_is_failed():
    rec = Recorder()
    with pytest.raises(FatalStageError, match="YOUTUBE_TRANSCRIPT_UNAVAILABLE"):
        run_stage(StubClient(rec, transcripts=[]), rec)
    rec = Recorder()
    with pytest.raises(FatalStageError, match="YOUTUBE_TRANSCRIPT_UNAVAILABLE"):
        run_stage(StubClient(rec, transcripts=[FakeTranscript("en", False, "English", [("", 0.0, 1.0)])]), rec)


# ===================================================================== classification (real exceptions)
@pytest.mark.parametrize("error,code", [
    (ytdlp_download_error("[youtube] x: Video unavailable. This video has been removed"), "YOUTUBE_VIDEO_UNAVAILABLE"),
    (ytdlp_download_error("[youtube] x: Private video. Sign in if you've been granted access"), "YOUTUBE_VIDEO_UNAVAILABLE"),
    (ytdlp_download_error("[youtube] x: This video is no longer available because the YouTube account "
                          "associated with this video has been terminated."), "YOUTUBE_VIDEO_UNAVAILABLE"),
    (ytdlp_download_error("HTTP Error 404: Not Found", status=404), "YOUTUBE_VIDEO_UNAVAILABLE"),
    (ytdlp_download_error("HTTP Error 410: Gone", status=410), "YOUTUBE_VIDEO_UNAVAILABLE"),
    (ytdlp_download_error("[youtube] x: Sign in to confirm your age"), "YOUTUBE_AGE_RESTRICTED"),
])
def test_permanent_metadata_errors_are_fatal(error, code):
    classified = classify_metadata_error(error)
    assert isinstance(classified, FatalStageError) and classified.code == code and not classified.retryable


@pytest.mark.parametrize("error,cls,code,retry_after", [
    (ytdlp_download_error("HTTP Error 429", status=429, headers={"Retry-After": "7"}), RateLimited,
     "YOUTUBE_RATE_LIMITED", 7.0),
    (ytdlp_download_error("HTTP Error 429: Too Many Requests"), RateLimited, "YOUTUBE_RATE_LIMITED", None),
    (ytdlp_download_error("[youtube] x: Sign in to confirm you’re not a bot"), RateLimited, "YOUTUBE_BLOCKED", None),
    (ytdlp_download_error("HTTP Error 503", status=503), TransientError, "YOUTUBE_HTTP_503", None),
    (ytdlp_download_error("Unable to download webpage", transport=True), TransientError, "YOUTUBE_UNREACHABLE", None),
])
def test_temporary_metadata_errors_are_retryable(error, cls, code, retry_after):
    classified = classify_metadata_error(error)
    assert type(classified) is cls and classified.code == code and classified.retryable
    if cls is RateLimited:
        assert classified.retry_after_sec == retry_after and classified.provider is Provider.YOUTUBE


def test_transcript_library_errors():
    import youtube_transcript_api as yta
    assert classify_transcript_error(yta.VideoUnavailable("v")).code == "YOUTUBE_VIDEO_UNAVAILABLE"
    assert classify_transcript_error(yta.InvalidVideoId("v")).code == "YOUTUBE_VIDEO_UNAVAILABLE"
    assert classify_transcript_error(yta.AgeRestricted("v")).code == "YOUTUBE_AGE_RESTRICTED"
    assert classify_transcript_error(yta.VideoUnplayable("v", None, [])).code == "YOUTUBE_VIDEO_UNPLAYABLE"
    for blocked in (yta.RequestBlocked("v"), yta.IpBlocked("v")):
        err = classify_transcript_error(blocked)
        assert isinstance(err, RateLimited) and err.code == "YOUTUBE_BLOCKED"
    rl = classify_transcript_error(requests_http_error(429, {"Retry-After": "12"}))
    assert isinstance(rl, RateLimited) and rl.retry_after_sec == 12.0
    gone = classify_transcript_error(requests_http_error(404))
    assert isinstance(gone, FatalStageError) and gone.code == "YOUTUBE_VIDEO_UNAVAILABLE"
    assert isinstance(classify_transcript_error(requests_http_error(500)), TransientError)
    temp = classify_transcript_error(yta.PoTokenRequired("v"))
    assert isinstance(temp, TransientError) and temp.code == "YOUTUBE_TRANSCRIPT_TEMPORARY"
    net = classify_transcript_error(requests.ConnectionError("refused"))
    assert isinstance(net, TransientError) and net.code == "YOUTUBE_UNREACHABLE"


def test_stage_raises_classified_errors_without_upstream_text():
    rec = Recorder()
    secretish = ytdlp_download_error("[youtube] x: Video unavailable https://example.invalid/?token=abc")
    with pytest.raises(FatalStageError) as err:
        run_stage(StubClient(rec, info_error=secretish), rec)
    assert "example.invalid" not in str(err.value) and err.value.__cause__ is None and err.value.__suppress_context__


def test_transcript_languages_setting():
    assert get_settings(transcript_languages=" en , de ").transcript_language_list == ["en", "de"]
    for bad in ("", "en;rm -rf", "x" * 20):
        with pytest.raises(SystemExit):
            get_settings(transcript_languages=bad)
    assert get_settings().max_video_duration_sec == 1200
