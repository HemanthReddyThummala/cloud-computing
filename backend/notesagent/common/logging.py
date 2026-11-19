"""Structured JSON logging with secret redaction.

Every log line passes through :func:`redact`, so even an accidental log of a
token, connection string or ciphertext is masked before it reaches stdout.
"""
from __future__ import annotations

import logging
import re
import sys

from pythonjsonlogger.json import JsonFormatter

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Authorization headers
    (re.compile(r"(?i)\b(bearer\s+)[^\s\"',]+"), r"\1[REDACTED]"),
    # Notion tokens (internal "secret_", OAuth "ntn_", refresh "nrt_")
    (re.compile(r"\b(secret_|ntn_|nrt_)[A-Za-z0-9_\-]+"), r"\1[REDACTED]"),
    # Passwords inside connection strings
    (re.compile(r"(?i)\b((?:amqps?|postgres(?:ql)?|rediss?)://[^:/@\s]*:)[^@\s]+@"), r"\1[REDACTED]@"),
    # Google API keys, and any key=... URL query parameter (Gemini sends keys this way)
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED-GOOGLE-KEY]"),
    (re.compile(r"([?&]key=)[^&\s\"'#]+"), r"\1[REDACTED]"),
    # Fernet ciphertext (credential mailbox payloads)
    (re.compile(r"\bgAAAAA[A-Za-z0-9_\-]{20,}={0,2}"), "[REDACTED-CIPHERTEXT]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingJsonFormatter(JsonFormatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        return redact(super().format(record))


def setup_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RedactingJsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
            static_fields={"service": service},
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    for noisy in ("aiormq", "aio_pika", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
