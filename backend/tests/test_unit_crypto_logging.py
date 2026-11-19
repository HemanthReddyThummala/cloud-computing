"""Credential encryption, fingerprints and log redaction."""
import io
import logging
import uuid

import pytest
from cryptography.fernet import Fernet

from notesagent.common.config import Settings
from notesagent.common.crypto import CredentialCipher, CredentialError
from notesagent.common.logging import RedactingJsonFormatter, redact

TOKEN = "ntn_" + "b7" * 20


@pytest.fixture
def cipher():
    return CredentialCipher(Fernet.generate_key().decode())


def test_seal_and_open_roundtrip(cipher):
    job = uuid.uuid4()
    blob = cipher.seal(job, TOKEN)
    assert TOKEN.encode() not in blob and b"b7b7" not in blob
    assert cipher.open(job, blob) == TOKEN


def test_blob_is_bound_to_its_job(cipher):
    blob = cipher.seal(uuid.uuid4(), TOKEN)
    with pytest.raises(CredentialError):
        cipher.open(uuid.uuid4(), blob)


def test_blob_from_other_key_rejected(cipher):
    other = CredentialCipher(Fernet.generate_key().decode())
    job = uuid.uuid4()
    with pytest.raises(CredentialError):
        cipher.open(job, other.seal(job, TOKEN))


def test_missing_key_is_an_error():
    with pytest.raises(RuntimeError):
        CredentialCipher("")


def test_fingerprint_is_keyed_deterministic_and_hides_token(cipher):
    a = cipher.fingerprint({"u": 1, "notion_token": TOKEN})
    assert a == cipher.fingerprint({"notion_token": TOKEN, "u": 1})
    assert a != cipher.fingerprint({"u": 1, "notion_token": TOKEN + "x"})
    assert TOKEN not in a and len(a) == 64
    other = CredentialCipher(Fernet.generate_key().decode())
    assert a != other.fingerprint({"u": 1, "notion_token": TOKEN})


@pytest.mark.parametrize("raw,secret", [
    ("Authorization: Bearer abc.DEF-123", "abc.DEF-123"),
    ("token=ntn_abc123XYZ", "abc123XYZ"),
    ("token=secret_abc123XYZ", "abc123XYZ"),
    ("refresh=nrt_abc123XYZ", "abc123XYZ"),
    ("amqp://notes:s3cr3tpw@rabbitmq:5672/", "s3cr3tpw"),
    ("postgresql://notes:s3cr3tpw@postgres:5432/notes", "s3cr3tpw"),
    ("blob gAAAAABlZ" + "x" * 60, "x" * 60),
])
def test_redact(raw, secret):
    assert secret not in redact(raw)


def test_json_log_lines_are_redacted():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingJsonFormatter("%(message)s"))
    logger = logging.getLogger("redaction-test")
    logger.addHandler(handler)
    logger.propagate = False
    logger.warning("using %s", TOKEN, extra={"url": "amqp://u:topsecret@h/"})
    out = stream.getvalue()
    assert "b7b7b7" not in out and "topsecret" not in out and "[REDACTED]" in out


def test_invalid_configuration_error_never_shows_values(monkeypatch, capsys):
    from notesagent.common.config import get_settings
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:leakypassword99@h/db")
    monkeypatch.setenv("CREDENTIAL_TTL_HOURS", "not-a-number-secretvalue")
    with pytest.raises(SystemExit) as info:
        get_settings()
    message = str(info.value)
    assert "RABBITMQ_URL" in message and "CREDENTIAL_TTL_HOURS" in message
    assert "leakypassword99" not in message and "secretvalue" not in message
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_settings_repr_hides_secrets():
    s = Settings(database_url="postgresql://u:pw123secret@h/db", rabbitmq_url="amqp://u:pw123secret@h/",
                 job_payload_encryption_key=Fernet.generate_key().decode())
    assert "pw123secret" not in repr(s) and "pw123secret" not in str(s.model_dump())
