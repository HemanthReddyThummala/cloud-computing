# Session log — External services and credentials inspection (read-only)

- **Date:** 2026-11-19
- **Branch:** `phase-2`; HEAD = `main` = `phase1-baseline` = `49eed18`; 0 staged
- **Nature:** inspection only. No code, configuration, containers, databases, volumes or credentials
  changed. The only file written is this log. Secret values were never printed (only variable names and
  whether a value is set).

Evidence labels: **[REPO]** repository/configuration · **[DOC]** official/primary documentation read
today (2026-11-19) · **[NV]** not verified.

## 1. Files and sources examined
- **Repository:**
  - `PROJECT_SPEC.pdf` (§1, §3, §6, §8, §9, §10);
  - `docs/decisions.md` (ADR-01, 03, 06, 07, 08, 10);
  - `.env.example`;
  - the local `.env` (names and set/empty state only — no values printed except non-secret public
    settings such as model name and limits);
  - `docker-compose.yml`, `backend/notesagent/common/config.py`, `backend/requirements.in`;
  - the approved Phase 0 plan in the session transcript (§C.3 technology choices, §3.8 dependencies).
- **External documentation**, read via a web-fetch helper:
  - Gemini rate limits: `ai.google.dev/gemini-api/docs/rate-limits` → 301 to
    `aistudio.google.com/docs/rate-limits`.
  - Notion: `developers.notion.com/reference/request-limits`, `/docs/authorization`,
    `/docs/working-with-files-and-media`, `/docs/uploading-small-files`, `/reference/capabilities`.
  - `pypi.org/project/youtube-transcript-api` + its GitHub README;
    `developers.google.com/youtube/v3/docs/captions/download`.

## 2. Findings

### 2.1 Transcript extraction
- **Planned, not implemented [REPO]:** the Phase 0 plan §C.3 chose `youtube-transcript-api` for the
  timestamped transcript and `yt-dlp` metadata for the title and exact duration (no API key). §3.8
  lists `google-genai`, `youtube-transcript-api`, `yt-dlp` and `ffmpeg` as worker-only dependencies.
  None of these are in `requirements.in` or the Dockerfile yet. **No ADR records the transcript
  method** (only ADR-01 mentions yt-dlp).
- **youtube-transcript-api [DOC]:**
  - Needs no API key or OAuth.
  - Uses "an undocumented part of the YouTube API … called by the YouTube web-client", with "no
    guarantee that it won't stop working tomorrow".
  - Returns snippets with `start` and `duration` (seconds implied). Supports manual and auto-generated
    captions (manual preferred) and a language priority list.
  - **No documented rate limits.** YouTube blocks most cloud-provider IPs (`RequestBlocked` /
    `IpBlocked`), and "same can happen to … your self-hosted solution, if you are doing too many
    requests". Rotating residential proxies are suggested.
  - Cookie authentication is currently broken, so **age-restricted videos are not accessible**.
  - The README does not document the exceptions for missing/disabled transcripts or
    private/unavailable videos.
  - Latest version 1.2.5 (2026-11-05).
- **Official YouTube Data API `captions.download` [DOC]:** requires OAuth scope
  `youtube.force-ssl`/`youtubepartner` **and** "permission to edit the video"; quota cost 200 units.
  It cannot be used for arbitrary public videos.
- **yt-dlp** (duration, title, and later the frame stream URL): no key. Its rate limits and blocking
  behaviour were not researched in this session [NV].

### 2.2 Gemini
- **Configured [REPO]:** `GEMINI_MODEL=gemini-3.8-flash`, `GEMINI_THINKING_LEVEL=low` (ADR-06; free tier
  only, no billing, no automatic paid fallback). Key variable: `GEMINI_API_KEY`.
  - `.env`: `GEMINI_API_KEY` present but **empty**; `GEMINI_RPM_LIMIT/TPM/RPD` present but **empty**;
    `GEMINI_LIMIT_SAFETY_FACTOR=0.8`, `LLM_MAX_WAIT_SEC=60`, `TRANSCRIPT_CHUNK_TOKENS=12000`.
- **Limits [DOC]:**
  - Dimensions are RPM, TPM (input) and RPD (TPD for some models), applied **per project, not per API
    key**.
  - **No free-tier numbers are published for Flash models**; they are shown only in AI Studio
    (`https://aistudio.google.com/rate-limit`).
  - RPD resets at **midnight Pacific**.
  - "Specified rate limits are not guaranteed."
  - The page documents HTTP 429 `RESOURCE_EXHAUSTED` only for spend-based limits; for RPM/TPM/RPD it
    gives no status code or Retry-After/RetryInfo detail.
  - The redirected page does not repeat the EEA/UK/CH and data-use notes recorded in ADR-06.
- **Free-tier availability of `gemini-3.8-flash`:** recorded in ADR-06 from the pricing page
  (2026-11-18); not re-verified today [NV].

### 2.3 Notion
- **Design [REPO]:** ADR-07, public integration with OAuth.
  - The extension uses `chrome.identity.launchWebAuthFlow`; the **gateway** performs the code
    exchange and refresh (the secret never reaches the extension).
  - Redirect candidate: `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion` (extension
    ID from ADR-08).
  - Default destination: `NOTION_DEFAULT_PARENT_ID` (a "Video Notes" page chosen in the page picker).
  - `.env`: `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI` and
    `NOTION_DEFAULT_PARENT_ID` are present but **empty**; `NOTION_VERSION=2026-03-11`,
    `NOTION_RPS_LIMIT=2.5`.
- **OAuth [DOC]:**
  - Create the connection at `app.notion.com/developers/connections` with redirect URI(s) and
    installation scope "Any workspace" (cannot be changed after creation).
  - It provides a client ID, a client secret and an authorization URL
    (`https://api.notion.com/v1/oauth/authorize?owner=user&client_id=…&redirect_uri=…&response_type=code`).
  - Token exchange: `POST /v1/oauth/token` with HTTP Basic `client_id:client_secret`.
  - The response includes `access_token`, a **`refresh_token`** (refreshing returns new access and
    refresh tokens), `bot_id` and `workspace_id`. The access-token lifetime is not stated.
  - Marketplace review is not required.
  - **The docs say nothing about allowed redirect-URI forms** (https, chromiumapp.org, localhost), so
    ADR-07's candidate is still **unverified**.
- **Page access [DOC]:** the user picks pages in the OAuth page picker (only pages with full access).
  Granting a parent page grants its children. Pages are chosen during authorization; if the flow is not
  completed, no access is granted.
- **Capabilities [DOC]:**
  - Insert content (create pages, add blocks, create/send file uploads);
  - Read content (check results, find a page by marker for idempotent resume);
  - Update content (needed only for updating existing blocks/properties);
  - user information: "No user information" is sufficient for this design [inference].
  - Changing capabilities later forces users to re-authorize.
- **Limits [DOC]:**
  - **180 requests/minute per connection** (avg 3/s) on non-Business plans (600/min on
    Business/Enterprise), as a 60 s budget that may be spent in bursts.
  - An additional **per-workspace** limit shared by all connections (number not published).
  - 429 `rate_limited` with `Retry-After` in integer seconds (≤ 60 s for the per-connection limit,
    possibly longer for the workspace limit); 529 `service_overload` is retryable.
  - Payload ≤ 1000 blocks and 500 KB; arrays ≤ 100 elements; rich-text content ≤ 2000 characters.
  - A 503 may still have committed the write (`committed_resource_id`).
  - File uploads: single-part ≤ 20 MB; **free workspaces 5 MiB per file**, paid 5 GiB. Unattached
    uploads expire after 1 hour. JPEG/PNG/WebP are supported.

### 2.4 Configuration wiring [REPO]
- None of the external-service variables (`GEMINI_*`, `NOTION_*`, `TRANSCRIPT_CHUNK_TOKENS`,
  `MAX_FRAMES`, `MAX_VIDEO_DURATION_SEC`, `PUBLIC_BASE_URL`, `ALLOWED_EXTENSION_ORIGIN`) is read by
  `config.py` or passed to any container by `docker-compose.yml`.
- They exist only in `.env.example` and `.env`. Wiring them up is part of the implementation phases.
- Not in `.env.example`, and **not required** by the current design: a YouTube API key, proxy
  settings, a cookies file, a transcript-language preference (optional design choice).

### 2.5 Rate limiting across the two workers (design per ADR-06, Redis already deployed)
- **Gemini:**
  - Atomic Redis (Lua) token buckets shared by every worker: RPM, input-TPM (estimated before the
    call, corrected after) and RPD (keyed by the Pacific-time date). Capacities are the AI Studio values
    × `GEMINI_LIMIT_SAFETY_FACTOR`.
  - Plus a shared cooldown key set on any 429.
  - Redis down → fail closed (`RateLimiterUnavailable`, infra budget).
  - The limits are per project, so all workers and any other app on the same project share them.
- **Notion:** one bucket at `NOTION_RPS_LIMIT` (2.5/s, below the documented 3/s average), plus a shared
  cooldown honouring `Retry-After`. The documented limit is per connection; the workspace limit is
  unknown, so a single global bucket is the conservative choice for one user.
- **YouTube:** no documented limits, so no bucket. Rely on low concurrency (2 workers, 1 job each) and
  classified backoff for 429 and blocking responses. Blocking may be persistent rather than
  rate-shaped [NV].

## 3. Unresolved questions
1. The Gemini free-tier RPM/TPM/RPD for `gemini-3.8-flash` on the user's project (AI Studio only); and
   whether the model is still free-tier on that project.
2. Whether Notion accepts the `chromiumapp.org` redirect URI (needs a real test, with approval).
3. yt-dlp / youtube-transcript-api behaviour from Docker on the user's home network (blocking risk) [NV].
4. The exact library exceptions for missing transcripts / private videos (needed for classification and
   for the DLQ demo decision D-1, still open from the compliance audit).
5. The transcript method is not recorded in an ADR.

## 4. Changes
None. Only this log was created. Git: HEAD `49eed18`, 0 staged, nothing committed.
