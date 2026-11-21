# Gemini hostname connectivity diagnostic

Date: 2026-11-21, 15:48 IST. Read-only network checks only; no Gemini API request was sent.

## Scope and method

Inspected the existing `docker-compose.yml`. The `worker` service has two running replicas, both
healthy on image `notes-agent-backend:dev`:

- `notes-agent-worker-1`
- `notes-agent-worker-2`

Compose inspection command:

```powershell
docker compose ps --format json
```

Ran the same Python standard-library diagnostic inside each already-running worker with
`docker exec`. It resolved the hostname using `socket.getaddrinfo` (10-second DNS alarm), attempted
a TCP connection to port 443 (10-second socket timeout), then made a separate TCP connection and
performed a TLS handshake (10-second socket timeout). TLS used
`ssl.create_default_context()` with SNI set to `generativelanguage.googleapis.com`, so normal CA
and hostname verification remained enabled. The reported TLS latency is handshake time after the
separate TCP connect.

The PowerShell invocation used a temporary in-memory Python script (`$probe`), encoded it as UTF-8
hex (`$hex`), and ran:

```powershell
$code = 'exec(bytes.fromhex(__import__(chr(115)+chr(121)+chr(115)).argv[1]))'
docker exec notes-agent-worker-1 python -c $code $hex
docker exec notes-agent-worker-2 python -c $code $hex
```

The in-memory `$probe` Python script supplied as `$hex` was:

```python
import json, signal, socket, ssl, time

host = "generativelanguage.googleapis.com"

def emit(phase, **data):
    print(json.dumps({"phase": phase, **data}, sort_keys=True))

def alarm_handler(signum, frame):
    raise TimeoutError("DNS resolution exceeded 10 seconds")

signal.signal(signal.SIGALRM, alarm_handler)
started = time.perf_counter()
signal.alarm(10)
try:
    infos = socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
    signal.alarm(0)
    addrs = []
    for family, socktype, proto, canonname, sockaddr in infos:
        ip = sockaddr[0]
        if ip not in addrs:
            addrs.append(ip)
    emit("dns", result="success", ips=addrs,
         latency_ms=round((time.perf_counter()-started)*1000, 2))
except Exception as exc:
    signal.alarm(0)
    emit("dns", result="failure",
         error=type(exc).__name__ + ": " + str(exc),
         latency_ms=round((time.perf_counter()-started)*1000, 2))
    raise SystemExit(0)

info = next((x for x in infos if x[0] == socket.AF_INET), infos[0])
family, socktype, proto, canonname, sockaddr = info
selected_ip = sockaddr[0]
started = time.perf_counter()
try:
    tcp = socket.socket(family, socktype, proto)
    tcp.settimeout(10)
    tcp.connect(sockaddr)
    tcp.close()
    emit("tcp", result="success", ip=selected_ip, port=443,
         latency_ms=round((time.perf_counter()-started)*1000, 2))
except Exception as exc:
    try:
        tcp.close()
    except Exception:
        pass
    emit("tcp", result="failure", ip=selected_ip, port=443,
         error=type(exc).__name__ + ": " + str(exc),
         latency_ms=round((time.perf_counter()-started)*1000, 2))

started = time.perf_counter()
try:
    raw = socket.socket(family, socktype, proto)
    raw.settimeout(10)
    raw.connect(sockaddr)
    context = ssl.create_default_context()
    tls = context.wrap_socket(raw, server_hostname=host, do_handshake_on_connect=False)
    handshake_started = time.perf_counter()
    tls.do_handshake()
    handshake_ms = round((time.perf_counter()-handshake_started)*1000, 2)
    version = tls.version()
    tls.close()
    emit("tls", result="success", ip=selected_ip, latency_ms=handshake_ms,
         negotiated_version=version,
         certificate_verification="passed (default certificate verification enabled)")
except Exception as exc:
    try:
        raw.close()
    except Exception:
        pass
    emit("tls", result="failure", ip=selected_ip,
         error=type(exc).__name__ + ": " + str(exc),
         latency_ms=round((time.perf_counter()-started)*1000, 2),
         certificate_verification="failed" if isinstance(exc, ssl.SSLCertVerificationError)
         else "not completed")
```

The first attempt to pass the multi-line script directly via `python -c` failed before any network
test ran because PowerShell/native argument quoting removed Python string quotes. Exact sanitized
error:

```text
File "<string>", line 6
    raise TimeoutError(DNS
                      ^
SyntaxError: '(' was never closed
```

The script was then passed as UTF-8 hex as shown above; the recorded DNS, TCP, and TLS results are
from those successful executions. This invocation error was not a connectivity failure.

The diagnostic used `socket`, `ssl`, `signal`, `time`, and `json` only. No package was installed and
no application, environment, Compose, network, container, or Redis state was changed. The probe did
not read or use an API key, and did not send an HTTP/API request.

## Results

DNS returned the same eight IPv4 addresses from both containers:

`172.217.116.4`, `172.217.118.4`, `172.217.112.4`, `172.217.119.4`,
`172.217.115.4`, `172.217.114.4`, `172.217.113.4`, `172.217.117.4`.

The diagnostic selected the first IPv4 address (`172.217.116.4`) for TCP and TLS.

| Worker | DNS | TCP to selected IP:443 | TLS handshake | Certificate verification |
|---|---|---|---|---|
| `notes-agent-worker-1` | Success, 35.66 ms | Success, 13.74 ms | Success, TLS 1.3, 44.70 ms | Passed using default verification |
| `notes-agent-worker-2` | Success, 30.43 ms | Success, 20.50 ms | Success, TLS 1.3, 39.30 ms | Passed using default verification |

No phase failed; there were no network or certificate errors to report.

## Interpretation and unresolved issues

Both workers can resolve Google's hostname and establish a verified TLS connection to the selected
Google IP on port 443. This rules out a general DNS failure or inability to establish basic TCP/TLS
connectivity from these workers at the time of this check.

It does **not** show that a Gemini API request reaches or is served by the API, nor that generation,
authentication, model access, or the API's HTTP response path is healthy. In particular, it does not
explain the earlier direct SDK call that got no response within 300 seconds: that separate request's
progress after its own connection was not observed here.

Next, investigate the SDK/HTTP transport timeout path and relevant Google API/project availability
without sending another Gemini request. A later live API test would require separate approval and
could consume quota.

## Session log

This diagnostic and the clarification in `docs/PROJECT_STATUS_AND_HANDOVER.md` were recorded. No
application code or running-system configuration was changed. No tests were run because this was a
read-only diagnostic and documentation update.
