"""ADR-15 SUMMARIZING stage, unit level (no network, no infrastructure): structured output validation,
grounding, chunking/merging, limiter ordering, classification of REAL google-genai errors, key handling."""
import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest

from notesagent.common.config import get_settings
from notesagent.common.errors import (
    FatalStageError,
    Provider,
    QuotaExhausted,
    RateLimited,
    RateLimiterUnavailable,
    TransientError,
)
from notesagent.common.http_errors import next_pacific_midnight
from notesagent.worker.gemini import GeminiClient, GeminiReply, classify_gemini_error
from notesagent.worker.stages import StageContext
from notesagent.worker.summarize import (
    ChunkNotes,
    chunk_segments,
    estimate_tokens,
    make_summarize_stage,
    normalize_chunk,
    select_moments,
    transcript_line,
)
from tests.support.fake_gemini import QUOTA_BODY, api_error, chunk_answer, rate_limited_body

FAKE_KEY = "AIzaFAKE-unit-test-key-0123456789abcdef"


def segs(n, step=5.0):
    return [{"start": i * step, "duration": step - 0.5, "end": i * step + step - 0.5,
             "text": f"Sentence {i} about topic {i // 10}."} for i in range(n)]


class Recorder:
    def __init__(self):
        self.events = []


class FakeLimiter:
    def __init__(self, rec, error=None):
        self.rec, self.error = rec, error

    async def acquire(self, provider, tokens=0, **kw):
        self.rec.events.append(("acquire", provider.value, tokens))
        if self.error:
            raise self.error
        return f"permit-{len(self.rec.events)}"

    async def settle(self, permit, actual):
        self.rec.events.append(("settle", actual))


class StubGemini:
    """Replies built from the prompt (like tests.support.fake_gemini) unless ``replies`` are given."""

    def __init__(self, rec, replies=None, error=None):
        self.rec, self.replies, self.error, self.prompts = rec, list(replies or []), error, []

    async def generate(self, *, system, prompt, schema):
        self.rec.events.append(("generate",))
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        if self.replies:
            return self.replies.pop(0)
        import re
        idx = [int(n) for n in re.findall(r"^\[#(\d+) ", prompt, re.M)]
        body = chunk_answer(idx) if idx else {"summary": "Merged summary.", "key_points": ["A", "B", "A2"]}
        return GeminiReply(json.dumps(body), prompt_tokens=123, output_tokens=45, finish_reason="STOP", blocked=False)


def run(gemini, rec, segments, *, limiter_error=None, title="Lecture", **settings):
    stage = make_summarize_stage(get_settings(**settings), gemini)
    ctx = StageContext(job_id=uuid.uuid4(), job={"video_id": "v", "video_title": title},
                       checkpoints={"TRANSCRIBING": {"title": title, "transcript": {"segments": segments}}},
                       worker_id="t", _credential_reader=None, limiter=FakeLimiter(rec, limiter_error))
    return asyncio.run(stage(ctx)).checkpoint


def reply(body, **kw):
    return GeminiReply(json.dumps(body) if not isinstance(body, str) else body, prompt_tokens=kw.get("pt", 100),
                       output_tokens=10, finish_reason=kw.get("finish", "STOP"), blocked=kw.get("blocked", False))


# ===================================================================== success + structure
def test_short_transcript_one_request_with_grounded_timestamps_and_captions():
    rec = Recorder()
    gem = StubGemini(rec)
    cp = run(gem, rec, segs(6))
    assert cp["chunk_count"] == 1 and cp["gemini_requests"] == 1 and len(gem.prompts) == 1
    assert cp["model"] == "gemini-3.5-flash-lite" and cp["summary"].startswith("Part covering segments 0-5")
    assert cp["key_points"] == ["Key point from segment 0", "Key point from segment 5"]
    assert [(s["title"], s["start_sec"]) for s in cp["sections"]] == [("Section at 0", 0.0), ("Section at 3", 15.0)]
    ts = cp["important_timestamps"]
    assert [(t["timestamp_sec"], t["segment_index"]) for t in ts] == [(0.0, 0), (25.0, 5)]   # = segment starts
    for t in ts:
        assert t["caption"] and t["reason"] and t["transcript_excerpt"].startswith("Sentence")
    assert "not been extracted or visually checked" in cp["grounding"]
    assert cp["usage"] == {"prompt_tokens": 123, "output_tokens": 45}
    assert cp["validation"] == {"dropped_sections": 0, "dropped_timestamps": 0}


def test_limiter_permit_before_every_gemini_request_and_settle_after():
    rec = Recorder()
    run(StubGemini(rec), rec, segs(6))
    assert [e[0] for e in rec.events] == ["acquire", "generate", "settle"]
    assert rec.events[0][1] == "GEMINI" and rec.events[0][2] > 0 and rec.events[2] == ("settle", 123)


def test_prompt_contains_numbered_lines_with_global_indices():
    line = transcript_line(42, {"start": 125.4, "text": "Look at\nthis   diagram"})
    assert line == "[#42 02:05] Look at this diagram"


# ===================================================================== validation / malformed output
@pytest.mark.parametrize("bad", [
    reply("{not json"), reply({"summary": 5}), reply({"summary": "x", "key_points": []}),
    reply({"summary": "  ", "key_points": [], "sections": [], "visual_moments": []}),
    GeminiReply(None, 10, 0, "MAX_TOKENS", False),
])
def test_malformed_output_is_a_retryable_invalid_output(bad):
    rec = Recorder()
    with pytest.raises(TransientError) as err:
        run(StubGemini(rec, [bad]), rec, segs(4))
    assert err.value.code == "GEMINI_INVALID_OUTPUT" and err.value.provider is Provider.GEMINI


def test_blocked_output_fails_permanently():
    rec = Recorder()
    with pytest.raises(FatalStageError, match="GEMINI_OUTPUT_BLOCKED"):
        run(StubGemini(rec, [GeminiReply(None, 10, 0, "SAFETY", True)]), rec, segs(4))


def test_ungrounded_and_out_of_chunk_items_are_dropped_never_invented():
    notes = ChunkNotes.model_validate({
        "summary": "s", "key_points": ["k"],
        "sections": [{"title": "ok", "start_segment": 2, "points": ["p"]},
                     {"title": "outside", "start_segment": 9, "points": []},
                     {"title": "", "start_segment": 3, "points": []}],
        "visual_moments": [{"segment": 3, "caption": "c", "reason": "r"},
                           {"segment": 99, "caption": "invented", "reason": "r"},
                           {"segment": -1, "caption": "neg", "reason": "r"},
                           {"segment": 4, "caption": "", "reason": "r"}]})
    out = normalize_chunk(notes, range(0, 6), segs(10))
    assert [s["title"] for s in out["sections"]] == ["ok"] and out["dropped_sections"] == 2
    assert [m["segment_index"] for m in out["moments"]] == [3] and out["dropped_moments"] == 3
    assert out["moments"][0]["timestamp_sec"] == 15.0


def test_transcript_missing_is_fatal():
    rec = Recorder()
    with pytest.raises(FatalStageError, match="TRANSCRIPT_MISSING"):
        run(StubGemini(rec), rec, [])


# ===================================================================== chunking + merging
def test_short_transcript_is_not_chunked():
    assert chunk_segments(segs(200), 1_000_000) == [range(0, 200)]


def test_chunks_are_ordered_contiguous_and_within_budget():
    s = segs(300)
    chunks = chunk_segments(s, 1000)
    assert len(chunks) > 1 and chunks[0].start == 0 and chunks[-1].stop == 300
    assert all(a.stop == b.start for a, b in zip(chunks, chunks[1:]))         # no gap, no overlap
    for c in chunks:
        assert sum(estimate_tokens(transcript_line(i, s[i])) + 1 for i in c) <= 1000


def test_long_transcript_chunks_merge_in_order_with_original_timestamps():
    rec = Recorder()
    gem = StubGemini(rec)
    cp = run(gem, rec, segs(300), transcript_chunk_tokens=1000, max_frames=8)
    n = cp["chunk_count"]
    assert n > 1 and cp["gemini_requests"] == n + 1 and len(gem.prompts) == n + 1
    # each chunk prompt holds only its own (global) segment numbers; the last request is the merge
    firsts = [int(p.split("[#", 1)[1].split(" ", 1)[0]) for p in gem.prompts[:n]]
    assert firsts == sorted(firsts) and firsts[0] == 0 and "[#" not in gem.prompts[-1]
    assert cp["summary"] == "Merged summary." and cp["key_points"] == ["A", "B", "A2"]
    starts = [s["start_sec"] for s in cp["sections"]]
    assert starts == sorted(starts) and starts[-1] > 1000                       # later chunks: real video time
    ts = [t["timestamp_sec"] for t in cp["important_timestamps"]]
    assert ts == sorted(ts) and len(ts) <= 8 and ts[-1] > 1000
    assert [e[0] for e in rec.events].count("acquire") == n + 1
    assert all(a[0] == "acquire" and b[0] == "generate" for a, b in zip(rec.events[::3], rec.events[1::3]))


def test_select_moments_dedupes_near_duplicates_and_spreads_the_cap():
    m = [{"timestamp_sec": t, "segment_index": i} for i, t in enumerate([0, 4, 30, 31, 60, 90, 120, 150, 180])]
    picked = select_moments(m, 3)
    assert [x["timestamp_sec"] for x in picked] == [0, 90, 180]               # 0/4 and 30/31 deduped, spread
    assert [x["timestamp_sec"] for x in select_moments(m, 20)] == [0, 30, 60, 90, 120, 150, 180]
    assert select_moments(m, 0) == []


# ===================================================================== errors (real SDK classes) + limiter
@pytest.mark.parametrize("exc,cls,code", [
    (api_error(429, rate_limited_body("7")), RateLimited, "GEMINI_RATE_LIMITED"),
    (api_error(429, QUOTA_BODY), QuotaExhausted, "GEMINI_QUOTA_EXHAUSTED"),
    (api_error(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}), TransientError, "GEMINI_HTTP_503"),
    (api_error(500, {"error": {"code": 500, "status": "INTERNAL"}}), TransientError, "GEMINI_HTTP_500"),
    (api_error(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}}), FatalStageError, "GEMINI_BAD_REQUEST"),
    (api_error(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}}), FatalStageError, "GEMINI_AUTH"),
    (api_error(404, {"error": {"code": 404, "status": "NOT_FOUND"}}), FatalStageError, "GEMINI_MODEL_NOT_FOUND"),
])
def test_sdk_errors_are_classified(exc, cls, code):
    err = classify_gemini_error(exc)
    assert type(err) is cls and err.code == code and err.provider is Provider.GEMINI


def test_retry_info_and_retry_after_are_honoured():
    assert classify_gemini_error(api_error(429, rate_limited_body("7"))).retry_after_sec == 7.0
    import httpx2
    from google.genai import errors
    with_header = errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
                                     response=httpx2.Response(429, headers={"Retry-After": "9"}))
    assert classify_gemini_error(with_header).retry_after_sec == 9.0


def test_daily_quota_resets_at_midnight_pacific():
    err = classify_gemini_error(api_error(429, QUOTA_BODY))
    assert err.resets_at == next_pacific_midnight(datetime.now(timezone.utc))


def test_network_errors_are_transient():
    import httpx2
    for exc in (httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow")):
        err = classify_gemini_error(exc)
        assert isinstance(err, TransientError) and err.code == "GEMINI_UNREACHABLE"


@pytest.mark.parametrize("error", [RateLimiterUnavailable("RATE_LIMITER_UNAVAILABLE", "x"),
                                   QuotaExhausted("GEMINI_DAILY_LIMIT_REACHED", "x", provider=Provider.GEMINI,
                                                  resets_at=next_pacific_midnight())])
def test_no_gemini_request_without_a_permit(error):
    rec = Recorder()
    gem = StubGemini(rec)
    with pytest.raises(type(error)):
        run(gem, rec, segs(4), limiter_error=error)
    assert gem.prompts == [] and [e[0] for e in rec.events] == ["acquire"]


def test_stage_reraises_classified_errors_without_upstream_text():
    rec = Recorder()
    leaky = api_error(400, {"error": {"code": 400, "message": f"API key not valid: {FAKE_KEY}"}})
    with pytest.raises(FatalStageError) as err:
        run(StubGemini(rec, error=leaky), rec, segs(4))
    assert FAKE_KEY not in str(err.value) and FAKE_KEY not in repr(err.value) and err.value.__suppress_context__


# ===================================================================== key handling / configuration
def test_missing_key_fails_before_any_permit():
    rec = Recorder()
    stage = make_summarize_stage(get_settings(gemini_api_key=""))            # real client path, no key
    ctx = StageContext(job_id=uuid.uuid4(), job={}, checkpoints={"TRANSCRIBING": {"transcript": {"segments": segs(3)}}},
                       worker_id="t", _credential_reader=None, limiter=FakeLimiter(rec))
    with pytest.raises(FatalStageError, match="GEMINI_NOT_CONFIGURED"):
        asyncio.run(stage(ctx))
    assert rec.events == []


def test_key_is_never_shown_by_settings_or_client():
    s = get_settings(gemini_api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(s) and FAKE_KEY not in str(s) and FAKE_KEY not in repr(GeminiClient(s))
    assert s.gemini_api_key.get_secret_value() == FAKE_KEY


def test_log_redaction_removes_google_keys():
    from notesagent.common.logging import redact
    assert FAKE_KEY not in redact(f"failed https://g.example/v1?key={FAKE_KEY}")
    assert FAKE_KEY not in redact(f"key {FAKE_KEY} rejected")


def test_configuration_defaults_and_validation():
    s = get_settings()
    assert (s.gemini_model, s.gemini_thinking_level, s.transcript_chunk_tokens, s.max_frames) == \
           ("gemini-3.5-flash-lite", "low", 12000, 8)
    for bad in ({"gemini_thinking_level": "turbo"}, {"gemini_model": "bad model!"},
                {"transcript_chunk_tokens": 100}, {"max_frames": -1}):
        with pytest.raises(SystemExit):
            get_settings(**bad)


def test_chunk_budget_never_exceeds_what_the_tpm_limit_can_grant():
    from notesagent.worker.summarize import chunk_budget
    assert chunk_budget(get_settings()) == 12_000
    assert chunk_budget(get_settings(transcript_chunk_tokens=199_000)) == 198_000       # TPM 200k - overhead
    assert chunk_budget(get_settings(gemini_tpm_limit=5_000)) == 3_000
