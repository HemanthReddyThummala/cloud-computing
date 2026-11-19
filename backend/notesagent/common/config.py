"""Runtime configuration, read only from environment variables.

Connection strings and keys are wrapped in ``SecretStr`` so that printing or
logging a ``Settings`` object never reveals passwords or keys.
"""
from __future__ import annotations

import os
import socket

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    service_name: str = "notesagent"
    log_level: str = "INFO"

    # Infrastructure
    database_url: SecretStr
    rabbitmq_url: SecretStr
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")

    # Security / credential mailbox (ADR-03)
    job_payload_encryption_key: SecretStr | None = None
    credential_ttl_hours: float = Field(24.0, gt=0)
    # How long a worker waits for a mailbox message that exists but is temporarily held
    # (unacked) by a crashed worker, before failing the job. Must exceed the time RabbitMQ
    # needs to detect a dead connection (AMQP heartbeat, 60 s by default).
    credential_read_wait_sec: float = Field(90.0, ge=0)

    # Gateway
    idempotency_key_ttl_hours: float = Field(24.0, gt=0)
    max_request_bytes: int = Field(16_384, ge=1_024)

    # Broker / outbox
    queue_delivery_limit: int = Field(10, ge=1)
    outbox_poll_interval_ms: int = Field(1000, ge=50)
    outbox_heartbeat_file: str = "/tmp/outbox-heartbeat"

    # Workers
    worker_id: str = ""
    worker_heartbeat_ttl_sec: int = Field(15, ge=5)
    worker_heartbeat_file: str = "/tmp/worker-heartbeat"
    lock_busy_retry_delay_sec: float = Field(2.0, ge=0)
    unexpected_error_retry_delay_sec: float = Field(2.0, ge=0)  # interim; Phase 2 => backoff schedule

    @property
    def effective_worker_id(self) -> str:
        return self.worker_id or f"{socket.gethostname()}-{os.getpid()}"

    @property
    def credential_ttl_ms(self) -> int:
        return int(self.credential_ttl_hours * 3600 * 1000)


class MigrationSettings(BaseSettings):
    """The migration job only needs the database."""
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    database_url: SecretStr
    log_level: str = "INFO"


def load_settings(cls: type[BaseSettings], **overrides) -> BaseSettings:
    """Build settings; on error report only field names and error types.

    Pydantic's default error text includes the (truncated) input values, which could
    expose parts of connection strings or keys in logs — so it is never shown.
    """
    try:
        return cls(**overrides)
    except ValidationError as exc:
        problems = ", ".join(f"{'.'.join(map(str, e['loc'])).upper()} ({e['type']})"
                             for e in exc.errors(include_input=False, include_url=False))
        raise SystemExit(f"Invalid configuration: {problems}") from None


def get_settings(**overrides) -> Settings:
    """Build settings from the environment; keyword overrides win (used by tests)."""
    return load_settings(Settings, **overrides)
