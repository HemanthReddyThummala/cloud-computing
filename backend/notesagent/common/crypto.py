"""Encryption for the per-job credential mailbox (ADR-03) and keyed fingerprints (ADR-04)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class CredentialError(Exception):
    """A credential blob could not be decrypted or does not belong to this job."""


class CredentialCipher:
    def __init__(self, fernet_key: str) -> None:
        if not fernet_key:
            raise RuntimeError("JOB_PAYLOAD_ENCRYPTION_KEY is not configured")
        self._fernet = Fernet(fernet_key.encode("ascii"))
        raw = base64.urlsafe_b64decode(fernet_key.encode("ascii"))
        # Separate key for fingerprints, derived from the configured key.
        self._hmac_key = hmac.new(raw, b"notesagent/idempotency-fingerprint/v1", hashlib.sha256).digest()

    def seal(self, job_id: uuid.UUID, notion_token: str) -> bytes:
        """Encrypt the token, bound to its job id so blobs cannot be swapped between jobs."""
        payload = json.dumps({"v": 1, "job_id": str(job_id), "notion_token": notion_token}).encode("utf-8")
        return self._fernet.encrypt(payload)

    def open(self, job_id: uuid.UUID, blob: bytes) -> str:
        try:
            data = json.loads(self._fernet.decrypt(blob))
        except (InvalidToken, ValueError) as exc:
            raise CredentialError("credential blob could not be decrypted") from exc
        if data.get("job_id") != str(job_id):
            raise CredentialError("credential blob belongs to a different job")
        return data["notion_token"]

    def fingerprint(self, fields: dict[str, Any]) -> str:
        """Keyed hash of a request; the token cannot be recovered from it."""
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._hmac_key, canonical, hashlib.sha256).hexdigest()
