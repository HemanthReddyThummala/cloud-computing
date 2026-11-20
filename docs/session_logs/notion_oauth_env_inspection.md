# Session log — Notion OAuth configuration and environment-variable inspection (read-only)

- **Date:** 2026-11-20 22:04 (host clock)
- **Branch:** `phase-2`; HEAD `1aa6863`; 0 staged. No files modified except this new log; no Git
  operations.
- **Secrets:** `.env` was parsed locally; only "missing / present, empty / present, has a value" and
  yes/no comparisons with non-secret constants were printed. No values were displayed or copied.

## 1. Files inspected
- `.env` (presence only), `.env.example`
- `backend/notesagent/common/config.py` (the `Settings` used by gateway, outbox publisher and workers)
- `backend/notesagent/gateway/` (all modules) and `backend/notesagent/worker/` (search for OAuth / Notion
  configuration)
- `docker-compose.yml` (per-service `environment:` blocks; services receive only variables listed
  explicitly, there is no `env_file:`)

## 2. Findings

### 2.1 Presence of the variables
| Variable | `.env` | `.env.example` |
|---|---|---|
| `NOTION_CLIENT_ID` | present, **empty** | present, empty (placeholder) |
| `NOTION_CLIENT_SECRET` | present, **empty** | present, empty (placeholder, marked `[SECRET]`) |
| `NOTION_REDIRECT_URI` | present, **empty** | present, empty ("set only after redirect-URI verification") |
| `NOTION_DEFAULT_PARENT_ID` | present, **empty** | present, empty ("page ID of your Video Notes page") |
| `NOTION_VERSION` | present, has a value | `2026-03-11` |
| `NOTION_RPS_LIMIT` | present, has a value | `2.5` |
| `PUBLIC_BASE_URL` | present, has a value | `http://localhost:8000` |
| `ALLOWED_EXTENSION_ORIGIN` | present, has a value; **equals `chrome-extension://kkkeigfecmfhmainhbkcpdhjnloifbdf`** (not the placeholder) | `chrome-extension://<EXTENSION_ID>` |

### 2.2 Does the gateway receive and read them?
- **Compose:** the `gateway` service passes only `DATABASE_URL`, `RABBITMQ_URL`, `REDIS_URL`,
  `JOB_PAYLOAD_ENCRYPTION_KEY`, `CREDENTIAL_TTL_HOURS`, `IDEMPOTENCY_KEY_TTL_HOURS`,
  `QUEUE_DELIVERY_LIMIT`, `BACKOFF_SCHEDULE_SEC`, `LOCK_RETRY_DELAY_SEC`, `INFRA_RETRY_DELAY_SEC`,
  `LOG_LEVEL`. **No `NOTION_*`, no `PUBLIC_BASE_URL`, no `ALLOWED_EXTENSION_ORIGIN`.** The only Notion
  variable passed anywhere is `NOTION_RPS_LIMIT` (worker service).
- **`config.py`:** the only Notion setting is `notion_rps_limit`. There are no fields for
  `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `NOTION_REDIRECT_URI`, `NOTION_DEFAULT_PARENT_ID`,
  `NOTION_VERSION`, `PUBLIC_BASE_URL` or `ALLOWED_EXTENSION_ORIGIN` (`extra="ignore"`: unknown variables
  are silently ignored).
- **Gateway code:** no OAuth code-exchange or refresh endpoint, no CORS configuration, and no reference
  to any of these variables.
- **Conclusion:** the four OAuth variables are **neither received nor read** by the gateway (nor by the
  worker). Filling them in `.env` is safe but has **no effect** until the gateway OAuth endpoints, the
  settings fields and the Compose wiring are implemented (Phase C / PUBLISHING work; not done here).

## 3. Variables the user should edit manually (local `.env` only, never in chat)
- `NOTION_CLIENT_ID`: from the "Video Notes Agent" integration (Configuration tab).
- `NOTION_CLIENT_SECRET`: same place; secret.
- `NOTION_REDIRECT_URI`: exactly the string printed by the extension's service-worker console
  (`showNotionRedirectUri()`), and registered identically in the Notion integration.
- `NOTION_DEFAULT_PARENT_ID`: the page ID of the "Video Notes" page (the 32-hex ID from the page URL;
  dashes optional), shared with the integration during authorization.
- `ALLOWED_EXTENSION_ORIGIN` already has the correct value; `.env.example` does not need editing.

## 4. Unresolved
- Gateway OAuth endpoints (code exchange/refresh with HTTP Basic client credentials), CORS for
  `ALLOWED_EXTENSION_ORIGIN`, `Settings` fields and Compose wiring do not exist yet.
- The exact redirect URI is not yet read from Chrome, and Notion's acceptance of a `chromiumapp.org`
  URI is unverified (`extension_redirect_uri_probe.md`).
- The worker will need `NOTION_DEFAULT_PARENT_ID` and `NOTION_VERSION` for PUBLISHING; it does not
  receive them yet.

## 5. Git
No staging, commit or push. HEAD `1aa6863`; 0 staged.
