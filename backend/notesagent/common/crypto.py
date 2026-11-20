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


SEALED_TOKEN_PREFIX = "nva1."


class TokenSealer:
    """Sealed Notion OAuth tokens (ADR-18): what the extension stores and sends as ``notion_token``.

    The spec has the extension keep the token and send it with every job, so the gateway never stores
    it; the raw access token, however, is never returned to the browser. The gateway returns
    ``nva1.<Fernet ciphertext>`` instead and unseals it on ``POST /jobs`` before the token goes into the
    per-job mailbox (ADR-03). The key is derived (HKDF-SHA256) from JOB_PAYLOAD_ENCRYPTION_KEY with its own
    purpose label, so sealed tokens and mailbox blobs can never be swapped.
    """

    def __init__(self, fernet_key: str) -> None:
        if not fernet_key:
            raise RuntimeError("JOB_PAYLOAD_ENCRYPTION_KEY is not configured")
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        raw = base64.urlsafe_b64decode(fernet_key.encode("ascii"))
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                       info=b"notesagent/notion-oauth-token/v1").derive(raw)
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def seal(self, access_token: str, workspace_id: str | None = None) -> str:
        payload = json.dumps({"v": 1, "access_token": access_token, "workspace_id": workspace_id}).encode("utf-8")
        return SEALED_TOKEN_PREFIX + self._fernet.encrypt(payload).decode("ascii")

    @staticmethod
    def is_sealed(value: str) -> bool:
        return value.startswith(SEALED_TOKEN_PREFIX)

    def unseal(self, sealed: str) -> str:
        if not self.is_sealed(sealed):
            raise CredentialError("not a sealed token")
        try:
            data = json.loads(self._fernet.decrypt(sealed[len(SEALED_TOKEN_PREFIX):].encode("ascii")))
        except (InvalidToken, ValueError, UnicodeEncodeError) as exc:
            raise CredentialError("sealed token could not be decrypted") from exc
        token = data.get("access_token") if isinstance(data, dict) else None
        if data.get("v") != 1 or not isinstance(token, str) or not token:
            raise CredentialError("sealed token has an unexpected format")
        return token
