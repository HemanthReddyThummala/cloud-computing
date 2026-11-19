"""Minimal RabbitMQ management HTTP API client (stdlib only).

Reads queues/bindings/policies and — for the work-queue migration only — writes and deletes
policies. Credentials and vhost are taken from the AMQP URL; they are sent only as an HTTP
Basic ``Authorization`` header and never appear in request URLs, exceptions or logs.

Failure model (callers fail closed on both):
  * ``ManagementUnavailable``      — transport error, timeout, 5xx, or an unusable response;
  * ``ManagementRequestRejected``  — the broker refused a write (4xx); carries the broker's reason.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote, unquote, urlsplit

_MAX_REASON = 200


class ManagementUnavailable(Exception):
    """The management API could not be reached or returned an unusable response."""


class ManagementRequestRejected(ManagementUnavailable):
    """The management API rejected a write request (HTTP 4xx)."""


class ManagementApi:
    def __init__(self, base_url: str, amqp_url: str, *, timeout: float = 5.0) -> None:
        parts = urlsplit(amqp_url)
        user, password = unquote(parts.username or ""), unquote(parts.password or "")
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self._base = base_url.rstrip("/")
        self.vhost = unquote(parts.path[1:]) if parts.path and parts.path != "/" else "/"
        self._timeout = timeout

    def __repr__(self) -> str:  # never expose the Authorization value
        return f"ManagementApi(base={self._base!r}, vhost={self.vhost!r})"

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self._base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310 - fixed scheme
                raw = response.read()
                return response.status, (json.loads(raw.decode("utf-8")) if raw else None)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, None
            if 400 <= exc.code < 500 and method != "GET":
                raise ManagementRequestRejected(f"management API rejected {method} (HTTP {exc.code}): "
                                                f"{self._reason(exc)}") from None
            raise ManagementUnavailable(f"management API returned HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ManagementUnavailable(f"management API unreachable ({type(exc).__name__})") from None
        except ValueError:
            raise ManagementUnavailable("management API returned invalid JSON") from None

    @staticmethod
    def _reason(exc: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            return str(payload.get("reason") or payload.get("error") or "no reason given")[:_MAX_REASON]
        except Exception:  # noqa: BLE001
            return "no reason given"

    def _get(self, path: str) -> Any | None:
        return self._request("GET", path)[1]

    def _vhost(self) -> str:
        return quote(self.vhost, safe="")

    @staticmethod
    def _q(name: str) -> str:
        return quote(name, safe="")

    # ------------------------------------------------------------ reads
    def queue(self, name: str) -> dict[str, Any] | None:
        data = self._get(f"/api/queues/{self._vhost()}/{self._q(name)}")
        if data is not None and not isinstance(data, dict):
            raise ManagementUnavailable("management API returned an unexpected queue document")
        return data

    def queues(self) -> list[dict[str, Any]]:
        data = self._get(f"/api/queues/{self._vhost()}?columns=name,policy")
        if not isinstance(data, list):
            raise ManagementUnavailable("management API returned an unexpected queue list")
        return data

    def bindings(self, exchange: str, queue: str) -> list[dict[str, Any]]:
        data = self._get(f"/api/bindings/{self._vhost()}/e/{self._q(exchange)}/q/{self._q(queue)}")
        if data is None:
            return []  # exchange or queue does not exist -> no bindings
        if not isinstance(data, list):
            raise ManagementUnavailable("management API returned an unexpected bindings document")
        return data

    def policy(self, name: str) -> dict[str, Any] | None:
        data = self._get(f"/api/policies/{self._vhost()}/{self._q(name)}")
        if data is not None and not isinstance(data, dict):
            raise ManagementUnavailable("management API returned an unexpected policy document")
        return data

    def policies(self) -> list[dict[str, Any]]:
        data = self._get(f"/api/policies/{self._vhost()}")
        if data is None:
            return []
        if not isinstance(data, list):
            raise ManagementUnavailable("management API returned an unexpected policy list")
        return data

    # ------------------------------------------------------------ writes (policies only)
    def put_policy(self, name: str, *, pattern: str, definition: dict[str, Any], priority: int,
                   apply_to: str) -> None:
        status, _ = self._request("PUT", f"/api/policies/{self._vhost()}/{self._q(name)}",
                                  {"pattern": pattern, "definition": definition, "priority": priority,
                                   "apply-to": apply_to})
        if status not in (200, 201, 204):
            raise ManagementUnavailable(f"unexpected HTTP {status} while setting policy")

    def delete_policy(self, name: str) -> bool:
        """True if deleted, False if it did not exist."""
        status, _ = self._request("DELETE", f"/api/policies/{self._vhost()}/{self._q(name)}")
        if status == 404:
            return False
        if status not in (200, 204):
            raise ManagementUnavailable(f"unexpected HTTP {status} while deleting policy")
        return True
