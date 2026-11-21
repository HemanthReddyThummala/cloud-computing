# Gemini SDK and HTTP transport investigation

Date: 2026-11-21, 15:52 IST. Read-only inspection only. No Gemini API/HTTP request was made.

## A. Verified facts

### Files and records inspected

- `docker-compose.yml` (worker environment and replica configuration)
- `backend/requirements.in` and `backend/requirements.txt`
- `backend/notesagent/common/config.py`
- `backend/notesagent/worker/gemini.py`
- `backend/notesagent/worker/summarize.py`
- `backend/tests/test_unit_gemini_sdk_contract.py` (mock transport only)
- `docs/PROJECT_STATUS_AND_HANDOVER.md`
- `docs/session_logs/gemini_network_connectivity_diagnostic.md`
- `docs/session_logs/gemini_minimal_plaintext_probe.md`
- `docs/session_logs/gemini_503_root_cause_investigation.md`
- `docs/session_logs/phaseB_part2_gemini_summarization.md`
- `docs/session_logs/gemini_controlled_tests_T1_T2.md`
- `docs/session_logs/gemini_integration_root_cause_diagnosis.md`
- `docs/session_logs/first_real_job_failure_diagnosis.md`

In the running image, inspected installed source/documentation in:

- `/usr/local/lib/python3.12/site-packages/google/genai/_api_client.py`
- `/usr/local/lib/python3.12/site-packages/google/genai/types.py`
- `/usr/local/lib/python3.12/site-packages/httpx2/_client.py`
- `/usr/local/lib/python3.12/site-packages/httpx2/_config.py`
- `httpx2.create_ssl_context()` and its `httpcore2`/`truststore` path

### Commands and method

Confirmed worker names and health:

```powershell
docker ps --filter "name=notes-agent-worker-" --format "{{.Names}} {{.Status}}"
```

For version, environment, resolver and TLS-context checks, ran read-only Python standard-library
and package-metadata snippets inside each existing container:

```powershell
docker exec notes-agent-worker-1 python -c $code $hex
docker exec notes-agent-worker-2 python -c $code $hex
```

The snippets were held in memory, UTF-8 hex encoded to avoid PowerShell argument quoting, and did
not print environment values for secrets. They read package metadata, selected non-secret environment
variable names, `/etc/resolv.conf`, Python SSL paths, SDK source and package defaults. The TLS probe
opened a TCP socket and completed a verified TLS handshake with `httpx2.create_ssl_context()`; it
sent **no HTTP bytes**:

```python
context = httpx2.create_ssl_context()
info = next(item for item in socket.getaddrinfo(
    host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM
) if item[0] == socket.AF_INET)
family, socktype, proto, _, address = info
raw = socket.socket(family, socktype, proto)
raw.settimeout(10)
raw.connect(address)
tls = context.wrap_socket(raw, server_hostname=host)
print(tls.version(), context.verify_mode, context.check_hostname)
```

No `.env` value or API key was read or printed. No package was installed, no container was restarted,
and no application, Compose, network, proxy, certificate, or Redis configuration was changed.

### Installed versions (same in both workers)

| Component | Version / result |
|---|---|
| Python | CPython 3.12.16 |
| `google-genai` | 3.6.1 |
| Gemini SDK HTTP backend | `httpx2` 2.16.2 |
| Backend core | `httpcore2` 2.16.2 |
| `httpx` / `httpcore` | 0.28.1 / 1.0.9 installed, but not selected for Gemini |
| `truststore` / `certifi` | 0.10.5 / 2026.10.23 |
| `aiohttp` | Not installed; it is not an alternate async transport in these workers |

`docker-compose.yml` passes `GEMINI_REQUEST_TIMEOUT_SEC` to both workers; each running container
has it set to `300`. `GOOGLE_GENAI_HTTP_CLIENT` and `GOOGLE_GENAI_API_VERSION` are unset in both.
The installed SDK source sets `_DEFAULT_HTTP_CLIENT_BACKEND = 'httpx2'` when
`GOOGLE_GENAI_HTTP_CLIENT` is unset, and the installed SDK resolved the production options to
`httpx2`. The SDK's default API version is `v1`.

The application code in `backend/notesagent/worker/gemini.py` constructs one lazy SDK client with
`HttpOptions(timeout=int(gemini_request_timeout_sec * 1000), retry_options=HttpRetryOptions(attempts=1))`.
It does not supply `api_version`, `client_args`, `async_client_args`, a custom HTTP client, transport,
proxy or SSL context. The SDK selects its async `httpx2` client. The separate installed `httpx`
package is used elsewhere in the project; its presence does not change the Gemini backend.

### Timeout behavior verified against installed SDK source

`google.genai.types.HttpOptions.timeout` is documented as milliseconds and defaults to `None`.
In `_api_client.py`, `get_timeout_in_seconds()` divides a nonzero value by 1000 and returns `None`
when unset. The SDK copies this value into the request timeout, passes it on the actual HTTP client
request, and adds `X-Server-Timeout` when a timeout is configured.

Offline checks of installed `httpx2` 2.16.2 showed:

- Its client default is `Timeout(5.0)`, with `trust_env=True`, `verify=True`, no explicit proxy, and
  connection-pool defaults of 100 maximum connections / 20 keep-alive connections / 5-second
  keep-alive expiry.
- `Timeout(None)` means no connect, read, write or pool timeout.
- `Timeout(300.0)` sets connect, read, write and pool phase timeouts to 300 seconds.
- The SDK's configured `HttpOptions.timeout=300000` maps to 300.0 seconds.
- The SDK request call has no separate application-level wall-clock timeout around `generate()`.
  The phase timeout is not a single stopwatch across all phases; the SDK also sends the 300-second
  `X-Server-Timeout` for the server.

The earlier minimal direct SDK probe explicitly omitted `HttpOptions.timeout`. Therefore the SDK
passed `timeout=None` for that HTTP request, overriding HTTPX2's otherwise-5-second client default
and disabling all four phase timeouts. Its recorded 300-second bound came from the probe's outer
`asyncio.wait_for(..., 300)`, which cancelled the call at about 300.1 seconds. There was no HTTP
status or response. This explains why the test waited for its full local limit; it does not explain
why no HTTP response arrived.

### Worker comparison: proxies, CA and DNS

| Setting | `notes-agent-worker-1` | `notes-agent-worker-2` |
|---|---|---|
| `HTTP_PROXY` / `http_proxy` | Unset | Unset |
| `HTTPS_PROXY` / `https_proxy` | Unset | Unset |
| `ALL_PROXY` / `all_proxy` | Unset | Unset |
| `NO_PROXY` / `no_proxy` | Unset | Unset |
| Proxy host information | None; no proxy URL was configured | None; no proxy URL was configured |
| `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` | All unset | All unset |
| Resolver | `127.0.0.11`, `options ndots:0` | `127.0.0.11`, `options ndots:0` |
| DNS result | Same 8 IPv4 addresses listed below | Same 8 IPv4 addresses listed below |
| SDK HTTP backend / timeout env | `httpx2`, timeout `300` | `httpx2`, timeout `300` |

The SDK source says HTTPX2 uses `httpx2.create_ssl_context()`, which delegates to `httpcore2` and
`truststore`'s OS-native trust store when no CA override is present. Both workers have the same
versions and unset CA override variables. A TLS-only handshake using this exact SDK context succeeded
in both workers with TLS 1.3, `CERT_REQUIRED`, hostname checking enabled, and no HTTP request:

- worker 1: `172.217.119.4`, 154.88 ms
- worker 2: `172.217.119.4`, 155.04 ms

The earlier standard-library TLS diagnostic also succeeded in both workers. The SDK-context check
establishes that a CA-bundle mismatch is not apparent in either running worker.

### Production, minimal probe and historical requests

- **Production summarization:** configured SDK timeout is 300 seconds (300000 ms), with one SDK
  attempt. That timeout applies to the actual HTTP request's HTTPX2 phase timeouts and also becomes
  the server deadline header; it is not merely an outer job/test wrapper.
- **Minimal plain-text probe:** explicitly used `api_version="v1"` and one attempt, but no SDK
  timeout. Its local 300-second `asyncio.wait_for` wrapper was the only request-duration bound.
  Therefore its transport configuration was not identical to production.
- **Earlier success versus first 503:** the existing investigation record says both used
  `google-genai` 3.6.1, the `httpx2` backend, `v1 generateContent`, the same request shape and a
  120-second server timeout. The successful request returned HTTP 200 in 32.7 seconds; the later
  request returned HTTP 503 in 4.29 seconds.
- **Later 503s:** existing records show HTTP 503 responses in about 1.7-69 seconds while using a
  300-second timeout. They were not local connect/read timeouts.
- **504s:** existing records show HTTP 504 responses at about 118.6-119.1 seconds when the configured
  server timeout was 120 seconds. That timing supports the prior diagnosis that the server deadline
  contributed to those 504s.

## B. Possible causes supported by evidence

1. The minimal probe intentionally had no SDK/HTTPX2 phase timeouts. Its outer cancellation at 300
   seconds explains the exact wait duration, but not the absence of an HTTP response.
2. The production SDK has 300-second phase timeouts and a 300-second server deadline, but no
   application-level total elapsed-time limit. This can permit a long wait up to the configured
   network-phase/server behavior; it does not explain a quick 503.
3. A response-path or transport issue after TLS remains possible. The raw verified TLS check proves
   the handshake, not that the SDK sent/received HTTP correctly.
4. HTTPX2 uses connection pooling. A stale connection or pool wait is possible in principle, but the
   minimal probe ran in a fresh throwaway container with one request, and there is no evidence of
   pool saturation or pool errors.
5. The 503s and 504s are recorded HTTP responses (not DNS/TCP/TLS failures). Their presence does not
   by itself identify whether the underlying cause is request-specific, service-side, transient
   infrastructure, or another API-path issue.

## C. Causes ruled out by current evidence

- A proxy configured through the standard proxy environment variables in either current worker.
- A mismatch between the current workers' proxy, CA override, DNS resolver, SDK/backend version, or
  Gemini timeout environment.
- A failure to establish verified TLS using either the Python default context or the SDK's own
  `httpx2` context.
- The minimal probe being governed by an SDK-level 300-second timeout: it was not; its 300-second
  cap was the outer wrapper.
- HTTPX2 pool exhaustion from concurrent traffic as an explanation for the one-request minimal
  probe in its fresh container.

These observations do **not** rule out all HTTP-client or SDK defects, an API response-path problem,
or a service-side issue.

## D. Unresolved questions

- Did the minimal probe's request finish its own TLS handshake and reach the Gemini HTTP endpoint?
  No response or request-level trace was recorded, and the separate TLS handshakes cannot answer this.
- What caused the server to return the recorded 503 `UNAVAILABLE` responses despite a prior
  successful, transport-equivalent request?
- Were the 504s caused solely by the `X-Server-Timeout` deadline or by a combination of request
  duration and server behavior? The timings strongly align with the configured deadline, but no
  additional request was made to test it.
- Is there a total wall-clock bound required in production beyond the existing per-phase timeouts
  and server deadline? That is a product/configuration decision; no code change is made here.

## Outcome and next step

No definite production configuration or implementation bug was identified, so no correction is
proposed or applied. The minimal probe's missing SDK timeout is a confirmed test-configuration
difference, not proof of the production cause. Prefer offline/mock-transport inspection next. Any
future live Gemini test needs separate approval and could consume quota.

No tests were run because this was a read-only runtime/source investigation and documentation update.
The root `handover.md` was not touched. No staging, commit, push or Git identity change was made.
