"""SUMMARIZING stage (ADR-15): Gemini study notes from the TRANSCRIBING checkpoint.

Grounding: the transcript is given to Gemini as numbered lines ``[#<index> <mm:ss>] text`` using the
segment's GLOBAL index in the whole transcript. Gemini answers with segment *numbers*, never free-form
times; every number is validated against the chunk it was given, and the timestamp stored is that
segment's real ``start`` (seconds from the start of the original video). Invented or out-of-range
numbers are dropped. Captions describe what the speaker presents and why it matters — they never
claim the frame was seen (frames are extracted and published later).

Chunking: the transcript lines are packed in order into chunks of at most ``TRANSCRIPT_CHUNK_TOKENS``
estimated tokens (whole segments, no overlap). One chunk -> one request. Several chunks -> one
request per chunk, then one small "merge" request that writes the overall summary and de-duplicated
key points from the chunk summaries (no transcript). Sections and timestamps are merged
deterministically in chronological order.

Every Gemini request first takes a ``Provider.GEMINI`` permit (estimated input tokens) from the shared
limiter and settles the real prompt-token count afterwards. Errors are classified (ADR-10) and
retried only by the existing outbox retry system.
"""
from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from notesagent.common.config import Settings
from notesagent.common.errors import FatalStageError, Provider, TransientError
from notesagent.worker.gemini import GeminiClient, classify_gemini_error, gemini_api_key, not_configured
from notesagent.worker.stages import StageContext, StageOutput

MIN_GAP_SEC = 10.0              # two important timestamps closer than this are duplicates
PROMPT_OVERHEAD_TOKENS = 2_000  # instructions + schema + header, estimated generously
MAX_KEY_POINTS = 12
MAX_POINTS_PER_SECTION = 8
GROUNDING_NOTE = ("Timestamps are transcript segment starts selected from what is said; the frames have not "
                  "been extracted or visually checked yet.")

SYSTEM = """You write accurate, well-structured study notes from a lecture or video transcript.
Rules:
- Use ONLY the transcript you are given. Do not add facts that are not in it.
- Every transcript line starts with [#<segment number> <mm:ss>]. Whenever you refer to a position,
  use a segment number that appears in the transcript lines.
- "sections": the video's parts in chronological order; "start_segment" is the segment where the
  part begins; "points" are concise bullet points.
- "visual_moments": moments where the speaker presents or refers to something visual (a diagram,
  slide, chart, table, code, equation, demonstration or on-screen text). Use the segment where it is
  introduced. "caption": one or two sentences explaining what is LIKELY shown, based on what is said,
  and why it matters. Never claim that you have seen the image; write e.g. "The speaker shows ...".
  "reason": why this moment was chosen, based on the words spoken in that segment.
  Return an empty list if nothing visual is referred to. Never invent segment numbers.
- Write in the language of the transcript."""

MERGE_SYSTEM = """You combine partial notes of consecutive parts of one video into the final overview.
Use ONLY the partial notes given. Keep the chronological order, merge duplicates, do not invent
anything. "summary": one coherent paragraph for the whole video. "key_points": the most important
points of the whole video, without duplicates, in the order they occur."""

CHUNK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "Concise summary of this transcript part."},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "sections": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "start_segment": {"type": "integer"},
                           "points": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "start_segment", "points"]}},
        "visual_moments": {"type": "array", "items": {
            "type": "object",
            "properties": {"segment": {"type": "integer"}, "caption": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["segment", "caption", "reason"]}},
    },
    "required": ["summary", "key_points", "sections", "visual_moments"],
}

MERGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "key_points": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "key_points"],
}


# ---------------------------------------------------------------- validation models (pydantic)
class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str
    start_segment: int
    points: list[str]


class _Moment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    segment: int
    caption: str
    reason: str


class ChunkNotes(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str
    key_points: list[str]
    sections: list[_Section]
    visual_moments: list[_Moment]


class MergedNotes(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str
    key_points: list[str]


# ---------------------------------------------------------------- helpers (pure)
def estimate_tokens(text: str) -> int:
    """Conservative estimate (≈3 characters per token); corrected after the call (limiter.settle)."""
    return max(1, math.ceil(len(text) / 3))


def mmss(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60:02d}:{rest % 60:02d}"


def transcript_line(index: int, segment: dict[str, Any]) -> str:
    text = " ".join(str(segment["text"]).split())
    return f"[#{index} {mmss(segment['start'])}] {text}"


def chunk_segments(segments: list[dict[str, Any]], budget_tokens: int) -> list[range]:
    """Consecutive, non-overlapping ranges of global segment indices, each within the token budget
    (a single over-long segment forms its own chunk). Order is preserved."""
    chunks, start, used = [], 0, 0
    for i, segment in enumerate(segments):
        cost = estimate_tokens(transcript_line(i, segment)) + 1
        if i > start and used + cost > budget_tokens:
            chunks.append(range(start, i))
            start, used = i, 0
        used += cost
    if segments:
        chunks.append(range(start, len(segments)))
    return chunks


def chunk_budget(settings: Settings) -> int:
    """TRANSCRIPT_CHUNK_TOKENS, but never more than a chunk the limiter can grant within the
    per-minute input-token limit (GEMINI_TPM_LIMIT minus the instruction overhead)."""
    return max(200, min(settings.transcript_chunk_tokens, settings.gemini_tpm_limit - PROMPT_OVERHEAD_TOKENS))


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _invalid(reason: str) -> TransientError:
    return TransientError("GEMINI_INVALID_OUTPUT", "Gemini returned notes that failed validation.",
                          provider=Provider.GEMINI, detail={"provider_code": reason})


def normalize_chunk(notes: ChunkNotes, allowed: range, segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate one chunk's notes against the segments it was given; drop ungrounded items."""
    summary = _clean(notes.summary, 2000)
    if not summary:
        raise _invalid("empty_summary")
    sections, dropped_sections = [], 0
    for s in notes.sections:
        title = _clean(s.title, 200)
        points = [p for p in (_clean(x, 400) for x in s.points) if p][:MAX_POINTS_PER_SECTION]
        if s.start_segment not in allowed or not title:
            dropped_sections += 1
            continue
        sections.append({"title": title, "start_segment": s.start_segment,
                         "start_sec": segments[s.start_segment]["start"], "points": points})
    sections.sort(key=lambda s: s["start_segment"])
    moments, dropped_moments = [], 0
    for m in notes.visual_moments:
        caption, reason = _clean(m.caption, 500), _clean(m.reason, 400)
        if m.segment not in allowed or not caption or not reason:
            dropped_moments += 1
            continue
        segment = segments[m.segment]
        moments.append({"timestamp_sec": segment["start"], "segment_index": m.segment, "caption": caption,
                        "reason": reason, "transcript_excerpt": _clean(segment["text"], 300)})
    key_points = [p for p in (_clean(x, 400) for x in notes.key_points) if p][:MAX_KEY_POINTS]
    return {"summary": summary, "key_points": key_points, "sections": sections, "moments": moments,
            "dropped_sections": dropped_sections, "dropped_moments": dropped_moments}


def select_moments(moments: list[dict[str, Any]], max_frames: int) -> list[dict[str, Any]]:
    """Chronological, de-duplicated (closer than MIN_GAP_SEC -> keep the first), and at most
    ``max_frames`` spread evenly over the video."""
    ordered = sorted(moments, key=lambda m: (m["timestamp_sec"], m["segment_index"]))
    unique: list[dict[str, Any]] = []
    for m in ordered:
        if not unique or m["timestamp_sec"] - unique[-1]["timestamp_sec"] >= MIN_GAP_SEC:
            unique.append(m)
    if max_frames <= 0:
        return []
    if len(unique) <= max_frames:
        return unique
    if max_frames == 1:
        return [unique[0]]
    picks = sorted({round(k * (len(unique) - 1) / (max_frames - 1)) for k in range(max_frames)})
    return [unique[i] for i in picks]


# ---------------------------------------------------------------- the stage
def make_summarize_stage(settings: Settings, client: Any | None = None):
    gemini = client

    async def ask(ctx: StageContext, system: str, prompt: str, schema: dict[str, Any], model: type[BaseModel],
                  usage: dict[str, int]):
        nonlocal gemini
        if gemini is None:
            if not gemini_api_key(settings):
                raise not_configured()
            gemini = GeminiClient(settings)
        tokens = estimate_tokens(system) + estimate_tokens(prompt) + estimate_tokens(json.dumps(schema))
        permit = await ctx.limiter.acquire(Provider.GEMINI, tokens=tokens)    # BEFORE every request
        try:
            if ctx.demo_faults is not None:                  # ADR-22: may raise a 429 INSTEAD of the request
                await ctx.demo_faults.before_gemini_request(ctx)
            reply = await gemini.generate(system=system, prompt=prompt, schema=schema)
        except Exception as exc:  # noqa: BLE001 - classified; upstream text is never kept
            raise classify_gemini_error(exc) from None
        usage["requests"] += 1
        if reply.prompt_tokens:
            usage["prompt_tokens"] += reply.prompt_tokens
            await ctx.limiter.settle(permit, reply.prompt_tokens)
        usage["output_tokens"] += reply.output_tokens or 0
        if reply.blocked:
            raise FatalStageError("GEMINI_OUTPUT_BLOCKED", "Gemini declined to summarize this video.",
                                  provider=Provider.GEMINI)
        if not reply.text:
            raise _invalid(f"no_text_{reply.finish_reason or 'unknown'}")
        try:
            return model.model_validate_json(reply.text)
        except ValidationError:
            raise _invalid("schema_mismatch") from None

    async def summarize(ctx: StageContext) -> StageOutput:
        transcribed = ctx.checkpoints.get("TRANSCRIBING") or {}
        segments = (transcribed.get("transcript") or {}).get("segments") or []
        if not segments:
            raise FatalStageError("TRANSCRIPT_MISSING", "The transcript of this job is missing.")
        title = transcribed.get("title") or ctx.job.get("video_title") or ""
        chunks = chunk_segments(segments, chunk_budget(settings))
        per_chunk = settings.max_frames if len(chunks) == 1 else math.ceil(settings.max_frames / len(chunks)) + 1
        usage = {"requests": 0, "prompt_tokens": 0, "output_tokens": 0}

        parts = []
        for number, chunk in enumerate(chunks, start=1):
            header = (f"Video title: {title}\n" if title else "") + (
                f"Transcript part {number} of {len(chunks)} (segments #{chunk.start}-#{chunk.stop - 1}).\n"
                f"Choose at most {per_chunk} visual_moments.\n\nTranscript:\n")
            prompt = header + "\n".join(transcript_line(i, segments[i]) for i in chunk)
            notes = await ask(ctx, SYSTEM, prompt, CHUNK_SCHEMA, ChunkNotes, usage)
            parts.append(normalize_chunk(notes, chunk, segments))

        if len(parts) == 1:
            summary, key_points = parts[0]["summary"], parts[0]["key_points"]
        else:
            prompt = (f"Video title: {title}\n\n" if title else "") + "\n\n".join(
                f"Part {n} ({mmss(segments[c.start]['start'])}-{mmss(segments[c.stop - 1]['start'])}):\n"
                f"Summary: {p['summary']}\nKey points:\n" + "\n".join(f"- {k}" for k in p["key_points"])
                for n, (c, p) in enumerate(zip(chunks, parts), start=1))
            merged = await ask(ctx, MERGE_SYSTEM, prompt, MERGE_SCHEMA, MergedNotes, usage)
            summary = _clean(merged.summary, 3000)
            key_points = [p for p in (_clean(x, 400) for x in merged.key_points) if p][:MAX_KEY_POINTS]
            if not summary:
                raise _invalid("empty_merged_summary")

        sections = [s for p in parts for s in p["sections"]]                 # chunk order = video order
        moments = select_moments([m for p in parts for m in p["moments"]], settings.max_frames)
        checkpoint = {
            "model": settings.gemini_model, "chunk_count": len(chunks), "gemini_requests": usage["requests"],
            "summary": summary, "key_points": key_points,
            "sections": [{"title": s["title"], "start_sec": s["start_sec"], "points": s["points"]} for s in sections],
            "important_timestamps": moments, "grounding": GROUNDING_NOTE,
            "usage": {"prompt_tokens": usage["prompt_tokens"], "output_tokens": usage["output_tokens"]},
            "validation": {"dropped_sections": sum(p["dropped_sections"] for p in parts),
                           "dropped_timestamps": sum(p["dropped_moments"] for p in parts)},
        }
        return StageOutput(checkpoint=checkpoint)

    return summarize
