# Session log — Phase B, Part 3: real YouTube frame extraction (EXTRACTING_FRAMES)

- **Started:** 2026-11-20 (host clock, see commands)
- **Branch:** `phase-2`; HEAD `1aa6863`; 0 staged; uncommitted Phase B Part 1 + 2 work preserved. No Git
  operations are performed.
- **Objective:** implement the EXTRACTING_FRAMES stage:
  - capture one genuine frame per Gemini-selected timestamp (SUMMARIZING checkpoint) from the source
    video with `yt-dlp` + FFmpeg;
  - graceful degradation (partial / all frames failed → continue with warnings, text-only possible);
  - a shared-limiter permit before every YouTube request;
  - errors through the existing retry/outbox system;
  - durable, idempotent checkpoint;
  - temporary files always cleaned up.
- **Out of scope:** Notion publishing, extension/OAuth.

## 1. Inspection and experiment
- **Start state:** HEAD `1aa6863`, 0 staged, 24 status entries (20:23).
- **Dockerfile:** `python:3.12-slim` base, no FFmpeg. The runner already merges stage warnings into the
  job and turns any warning into COMPLETED_WITH_WARNINGS at PUBLISHING; the lifecycle needed no change.
- **Experiment** (throwaway container from the current image, root only inside it):
  - `apt-get install --no-install-recommends ffmpeg` → **ffmpeg 7.1.5-0+deb13u1** (Debian 13.7), about
    **427 MB** added under `/usr`, `ffprobe` included;
  - `yt-dlp` without any JavaScript runtime printed the EJS deprecation warning but listed 24 formats
    (12 video-only) and downloaded a real video-only stream of `jNQXAC9IVRw` (218.53 KiB, 2.2 s, 240p);
  - FFmpeg cut a frame at 10.5 s and `ffprobe` reported a valid 320×240 MJPEG.
  - Conclusion: **no JS runtime is needed** today. It stays a documented risk, and the stage degrades if
    formats disappear.

## 2. Files created / modified
| File | Why |
|---|---|
| `backend/Dockerfile` | base stage: `apt-get install --no-install-recommends ffmpeg` (worker and test images); old "Phase 4 will add ffmpeg" comment replaced |
| `backend/notesagent/worker/frames.py` (new) | the stage: one permit + one download into a private temp dir; ffprobe duration; per-timestamp FFmpeg extraction + ffprobe validation; base64 JPEG checkpoint; warnings for failures; download-error policy (raise retryable/permanent, degrade others); cleanup in `finally` + purge of dead/zombie/old work dirs |
| `backend/notesagent/worker/youtube.py` | `YouTubeClient.download_video` (video-only ≤ height, H.264 preferred, no internal retries, `max_filesize`, no URL returned) |
| `backend/notesagent/worker/stages.py` | the registry adds EXTRACTING_FRAMES |
| `backend/notesagent/common/config.py` | `frame_source_max_height` 720, `frame_max_width` 1280, `frame_jpeg_quality` 3, `frame_ffmpeg_timeout_sec` 60, `frame_download_max_mb` 1024, `frame_work_dir`, `frame_tmp_max_age_sec` 6 h |
| `docker-compose.yml`, `.env.example` | `FRAME_SOURCE_MAX_HEIGHT`, `FRAME_MAX_WIDTH` |
| `docs/decisions.md`, `docs/api.md` | ADR-16; the frame warning shape |
| `backend/tests/support/fake_youtube.py` | `download_video` producing a REAL synthetic video with FFmpeg's `testsrc2` (scenarios: ok, 429 once, network, unavailable, format unavailable, unexpected, missing file, corrupt) + `make_test_video` |
| `backend/tests/support/run_youtube_worker.py` | docstring (fixtures only for PUBLISHING now) |
| `backend/tests/test_unit_frames.py` (new) | 42 unit cases with real FFmpeg |
| `backend/tests/test_frames_stage.py` (new) | 7 integration tests with the real worker |
| `backend/tests/test_worker.py`, `test_summarize_stage.py`, `test_transcribe_stage.py` | the production chain now stops at PUBLISHING; redelivery tests pause in the PUBLISHING fixture |

No new Python dependency. The summarization output contract is unchanged.

## 3. Tests
- **First focused run** (`pytest tests/test_unit_frames.py tests/test_frames_stage.py
  tests/test_summarize_stage.py tests/test_transcribe_stage.py tests/test_worker.py`, after rebuilding
  the test image, 21:04:08–21:05:47): **1 failed, 73 passed**.
  - The failure: `test_crash_during_download_repeats_only_the_frames_and_cleans_up` — the SIGKILLed
    worker's work dir was not purged.
  - Cause: the killed process was not yet reaped by the test harness (a zombie), and `os.kill(pid, 0)`
    succeeds for zombies.
  - **Fix:** `_process_alive()` also reads `/proc/<pid>/status` and treats state `Z` as dead (new unit
    test `test_zombie_owner_counts_as_dead`), and the integration test waits for the killed process,
    as a real crash ends it.
- **Same command after the fix:** **75 passed** in 88.13 s.
- **Full suite:** §5.

## 4. Real check outside the suite (one public video, no database writes)
- **Image:** `docker compose build migrate` → `notes-agent-backend:dev` `745413424d3d` (961 MB, was
  334 MB). **Not deployed.**
- **Method:** a throwaway container ran the production `make_frames_stage` with the **real `yt-dlp`
  client** (stand-in limiter counting permits) on `jNQXAC9IVRw`, with timestamps 1.2 s (the one real
  Gemini chose in Phase B Part 2), 10.0 s and 25.0 s (beyond the video).
- **Result:**
  - 2.7 s, **1 permit**;
  - source `format_id 133`, mp4, 320×240, `avc1.4D400C`, `duration_sec 18.933333`;
  - frames: t=1.2 (16,268 bytes) and t=10.0 (16,789 bytes), both valid JPEGs with different sha256,
    `caption_basis=transcript`, and the original Gemini caption on the first;
  - warning `{"code": "FRAME_EXTRACTION_FAILED", "timestamp_sec": 25.0, "reason": "TIMESTAMP_OUT_OF_RANGE"}`;
  - work dir empty afterwards.

## 5. Full suite
`MSYS_NO_PATHCONV=1 docker compose --profile test run --rm --no-deps tests pytest -p no:warnings -q -rfE --tb=short`
(21:08:30–21:19:30) → **797 passed** in 657.90 s, 0 failed, 0 errors. Previous: 748; +49 new
(42 unit + 7 integration). RabbitMQ/PostgreSQL/Redis healthy with 0 restarts; no new diagnostics files.

Acceptance coverage:
- **Extraction at the selected timestamps:** multiple frames, timestamp/caption/segment association,
  distinct real frames (sha256), never upscaled.
- **Validation:** invalid / negative / NaN / out-of-range timestamps; missing / empty / non-JPEG /
  undecodable output; corrupt source (FFMPEG_ERROR).
- **Degradation:** partial failures (COMPLETED_WITH_WARNINGS with the spec's warning shape in the
  real worker and in `/results`); all frames failing → text-only (format unavailable, unexpected
  download error, missing file).
- **Errors:** retryable 429 / network / 503 / bot check raised; permanent unavailable / 404 → FAILED;
  429 retried through the outbox in the real worker.
- **Limiter:** permit before the download; no permit → no download; no timestamps → no request.
- **Idempotency:** redelivery after the checkpoint → no second download; a crash during the download →
  only this stage repeats, TRANSCRIBING is not repeated.
- **Temporary files:** cleanup after success / raised error / degraded; purge of dead, zombie, own and
  old work dirs.
- **Image:** FFmpeg/ffprobe present and working in the image.

## 6. Unresolved issues and limitations
1. **Not deployed:** the dev image was rebuilt (961 MB) but the running containers still use the Phase
   B Part 1 image (no SUMMARIZING / EXTRACTING_FRAMES). Deploying needs an explicit instruction.
2. **No JS runtime:** yt-dlp works today without one but warns that this is deprecated. If YouTube
   withholds formats, every job degrades to text-only until `deno` is added.
3. **Download size and time:** one full video-only stream (≤ 720p) is downloaded per job. For a
   20-minute talk that is typically tens to a few hundred MB and some seconds to minutes; not measured
   on a long video. Downloading only short sections around each timestamp would be an optimisation.
4. **Frames in jsonb:** frames are stored as base64 JPEG in the checkpoint (typically 15–200 KB each,
   ≤ 8 frames). Acceptable for now; a separate table would be an optimisation.
5. **A frame is real but may not show what the caption describes** (the caption comes from the
   transcript). `caption_basis = "transcript"` marks this for PUBLISHING. There is no visual
   verification.
6. **Image size:** FFmpeg adds about 430 MB to the image.

## 7. Git
No Git operations were performed: no staging, commit, push or identity change. HEAD `1aa6863`;
0 staged; 29 status entries (Phase B Parts 1–3 uncommitted).
