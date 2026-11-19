"""Runtime configuration, read only from environment variables.

Connection strings and keys are wrapped in ``SecretStr`` so that printing or
logging a ``Settings`` object never reveals passwords or keys.
"""
from __future__ import annotations

import os
import socket

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
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
    # Delivery limit of jobs.dlq.audit (ADR-12). RabbitMQ carries a message's delivery count over
    # when it dead-letters it: a crash-looped job message reaches the audit queue with a count of
    # QUEUE_DELIVERY_LIMIT + 1. The audit limit must therefore be larger, or the first audit-consumer
    # crash would already park the message. Unset -> QUEUE_DELIVERY_LIMIT + 11 (10 audit crashes).
    audit_queue_delivery_limit: int | None = Field(None, ge=1)
    rabbitmq_management_url: str = "http://rabbitmq:15672"
    # Delay-queue topology (Phase 2). Part 1 only declares the queues; retry logic is Part 3.
    backoff_schedule_sec: str = "10,30,90"
    lock_retry_delay_sec: int = Field(5, ge=1)
    infra_retry_delay_sec: int = Field(30, ge=1)
    # Upper bound for a provider's relative Retry-After (seconds). Absolute quota resets are NOT capped.
    retry_after_max_sec: float = Field(3600.0, gt=0, le=86_400)
    # How often workers poll the legacy Phase 1 queue (jobs.work) while it still exists.
    legacy_drain_interval_sec: float = Field(5.0, gt=0)
    outbox_poll_interval_ms: int = Field(1000, ge=50)
    outbox_heartbeat_file: str = "/tmp/outbox-heartbeat"

    # Workers
    worker_id: str = ""
    worker_heartbeat_ttl_sec: int = Field(15, ge=5)
    worker_heartbeat_file: str = "/tmp/worker-heartbeat"
    # Retry budgets (Part 3): MAX_ATTEMPTS = total executions incl. the first (P3-D6).
    max_attempts: int = Field(5, ge=1)
    infra_max_attempts: int = Field(20, ge=1)

    # Shared external-API rate limiter (ADR-13). Gemini values are ENFORCED AS GIVEN: they already
    # include the safety margin (user's AI Studio limits for gemini-3.8-flash x 0.8). RPD resets at
    # midnight Pacific. Notion: requests per second per connection (documented average: 3/s).
    gemini_rpm_limit: int = Field(4, ge=1)
    gemini_tpm_limit: int = Field(200_000, ge=1)
    gemini_rpd_limit: int = Field(16, ge=1)
    notion_rps_limit: float = Field(2.5, gt=0, le=100)
    # Longest a worker waits in-process for the limiter; longer waits re-queue the job (ADR-11).
    rate_limit_max_wait_sec: float = Field(60.0, ge=0, le=600)
    # Shared cooldown after a 429 that carries no Retry-After.
    rate_limit_default_cooldown_sec: float = Field(10.0, gt=0, le=3600)

    @property
    def effective_worker_id(self) -> str:
        return self.worker_id or f"{socket.gethostname()}-{os.getpid()}"

    @property
    def credential_ttl_ms(self) -> int:
        return int(self.credential_ttl_hours * 3600 * 1000)

    @field_validator("backoff_schedule_sec")
    @classmethod
    def _valid_schedule(cls, value: str) -> str:
        parts = [p.strip() for p in value.split(",")]
        if not parts or not all(p.isdigit() and 1 <= int(p) <= 86_400 for p in parts):
            raise ValueError("must be a comma-separated list of whole seconds between 1 and 86400")
        return ",".join(parts)

    @model_validator(mode="after")
    def _audit_limit_exceeds_carried_count(self) -> "Settings":
        if (self.audit_queue_delivery_limit is not None
                and self.audit_queue_delivery_limit <= self.queue_delivery_limit + 1):
            raise ValueError("AUDIT_QUEUE_DELIVERY_LIMIT must be greater than QUEUE_DELIVERY_LIMIT + 1")
        return self

    @property
    def effective_audit_queue_delivery_limit(self) -> int:
        if self.audit_queue_delivery_limit is not None:
            return self.audit_queue_delivery_limit
        return self.queue_delivery_limit + 11

    @property
    def backoff_delays(self) -> list[int]:
        """Backoff steps in seconds; the last step repeats for later attempts."""
        return [int(p) for p in self.backoff_schedule_sec.split(",")]

    @property
    def delay_queue_seconds(self) -> list[int]:
        """Every delay for which a delay queue must exist."""
        return sorted(set(self.backoff_delays) | {self.lock_retry_delay_sec, self.infra_retry_delay_sec})


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
