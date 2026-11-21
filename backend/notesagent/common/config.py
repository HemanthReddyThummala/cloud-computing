"""Runtime configuration, read only from environment variables.

Connection strings and keys are wrapped in ``SecretStr`` so that printing or
logging a ``Settings`` object never reveals passwords or keys.
"""
from __future__ import annotations

import os
import re
import socket
import uuid

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

    # Shared external-API rate limiter (ADR-13). Gemini values are ENFORCED AS GIVEN and unchanged;
    # their safety margin was set for gemini-3.8-flash and has not been revalidated for Flash-Lite.
    # RPD resets at midnight Pacific. Notion: requests per second per connection (documented average: 3/s).
    gemini_rpm_limit: int = Field(4, ge=1)
    gemini_tpm_limit: int = Field(200_000, ge=1)
    gemini_rpd_limit: int = Field(16, ge=1)
    notion_rps_limit: float = Field(2.5, gt=0, le=100)
    # Longest a worker waits in-process for the limiter; longer waits re-queue the job (ADR-11).
    rate_limit_max_wait_sec: float = Field(60.0, ge=0, le=600)
    # Shared cooldown after a 429 that carries no Retry-After.
    rate_limit_default_cooldown_sec: float = Field(10.0, gt=0, le=3600)

    # TRANSCRIBING stage (ADR-14). Constraint A: longer videos are rejected (exactly the max is accepted).
    max_video_duration_sec: int = Field(1200, ge=1)
    # Preferred transcript languages, highest priority first; any other available transcript is used
    # only if none of these exists.
    transcript_languages: str = "en"
    # Timeout for each YouTube HTTP request made by the libraries.
    youtube_request_timeout_sec: float = Field(30.0, gt=0, le=300)

    # SUMMARIZING stage (ADR-06, ADR-15). Free tier only; the key is never logged (SecretStr).
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_thinking_level: str = "low"
    # Per-request timeout. The SDK also sends it to Google as the server deadline (X-Server-Timeout),
    # so a too-short value turns slow generations into HTTP 504 (2026-11-20 diagnosis, ADR-21).
    gemini_request_timeout_sec: float = Field(300.0, gt=0, le=600)
    # Backoff for Gemini 503 (UNAVAILABLE) / 504 (DEADLINE_EXCEEDED) only (ADR-21); last step repeats.
    # Other errors keep BACKOFF_SCHEDULE_SEC. Reached through the existing delay queues (uncounted hops).
    gemini_unavailable_backoff_sec: str = "60,180,600"
    gemini_max_output_tokens: int = Field(8192, ge=256, le=65_536)
    # Estimated transcript tokens per Gemini request; longer transcripts are split into chunks.
    transcript_chunk_tokens: int = Field(12_000, ge=500)
    # Upper bound of important timestamps (= frames to extract later) per video.
    max_frames: int = Field(8, ge=0, le=30)

    # EXTRACTING_FRAMES stage (ADR-16).
    frame_source_max_height: int = Field(720, ge=144, le=2160)     # downloaded video-only stream
    frame_max_width: int = Field(1280, ge=160, le=3840)            # stored JPEG width (never upscaled)
    frame_jpeg_quality: int = Field(3, ge=2, le=31)                # FFmpeg -q:v (2 = best, 31 = worst)
    frame_ffmpeg_timeout_sec: float = Field(60.0, gt=0, le=600)
    frame_download_max_mb: int = Field(1024, ge=10, le=10_240)     # yt-dlp max_filesize guard
    frame_work_dir: str = "/tmp/notesagent-frames"
    frame_tmp_max_age_sec: float = Field(6 * 3600, gt=0)            # leftovers older than this are purged

    # PUBLISHING stage (ADR-07, ADR-17). The per-job OAuth token comes from the credential mailbox.
    notion_version: str = "2026-03-11"
    notion_default_parent_id: str | None = None                    # page ID, used when the job has none
    notion_api_base_url: str = "https://api.notion.com/v1"         # tests point this to a local fake
    notion_request_timeout_sec: float = Field(30.0, gt=0, le=300)
    notion_blocks_per_request: int = Field(100, ge=1, le=100)      # Notion: <= 100 children per request

    # Notion OAuth (ADR-07, ADR-18) — gateway only. The client secret never leaves the gateway.
    notion_client_id: str | None = None
    notion_client_secret: SecretStr | None = None
    notion_redirect_uri: str | None = None                         # must equal the URI registered in Notion
    oauth_state_ttl_sec: int = Field(600, ge=30, le=3600)          # single-use OAuth state lifetime

    @field_validator("notion_client_id", "notion_redirect_uri", mode="before")
    @classmethod
    def _empty_is_unset(cls, value):
        return None if isinstance(value, str) and not value.strip() else (value.strip() if isinstance(value, str) else value)

    @field_validator("notion_client_secret", mode="before")
    @classmethod
    def _empty_secret_is_unset(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("notion_redirect_uri")
    @classmethod
    def _valid_redirect_uri(cls, value):
        if value is not None and (not value.startswith("https://") or "#" in value or any(c.isspace() for c in value)):
            raise ValueError("must be an absolute https URL without a fragment")
        return value

    @field_validator("notion_default_parent_id", mode="before")
    @classmethod
    def _valid_parent_id(cls, value):
        """A Notion page ID (32 hex, dashes optional) or a Notion page URL ending in that ID."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        text = str(value).strip()
        if text.startswith("https://"):
            match = re.search(r"([0-9a-fA-F]{32})(?:[?#].*)?$", text.split("?")[0].replace("-", ""))
            if not match:
                raise ValueError("a Notion page URL must end with the page's 32-character ID")
            text = match.group(1)
        text = text.replace("-", "")
        if len(text) != 32 or any(c not in "0123456789abcdefABCDEF" for c in text):
            raise ValueError("must be a Notion page ID (32 hex characters, dashes optional) or a Notion page URL")
        return str(uuid.UUID(text))

    @property
    def notion_oauth_configured(self) -> bool:
        return bool(self.notion_client_id and self.notion_client_secret and self.notion_redirect_uri)

    @property
    def effective_worker_id(self) -> str:
        return self.worker_id or f"{socket.gethostname()}-{os.getpid()}"

    @property
    def credential_ttl_ms(self) -> int:
        return int(self.credential_ttl_hours * 3600 * 1000)

    @field_validator("backoff_schedule_sec", "gemini_unavailable_backoff_sec")
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

    @field_validator("gemini_thinking_level")
    @classmethod
    def _valid_thinking_level(cls, value: str) -> str:
        if value.strip().lower() not in {"minimal", "low", "medium", "high"}:
            raise ValueError("must be one of minimal, low, medium, high")
        return value.strip().lower()

    @field_validator("gemini_model")
    @classmethod
    def _valid_model(cls, value: str) -> str:
        if not value.strip() or not all(c.isalnum() or c in "-._" for c in value.strip()):
            raise ValueError("must be a Gemini model name such as gemini-3.5-flash-lite")
        return value.strip()

    @field_validator("transcript_languages")
    @classmethod
    def _valid_languages(cls, value: str) -> str:
        parts = [p.strip() for p in value.split(",") if p.strip()]
        if not parts or not all(p.replace("-", "").isalnum() and len(p) <= 16 for p in parts):
            raise ValueError("must be a comma-separated list of language codes, e.g. 'en' or 'en,de'")
        return ",".join(parts)

    @property
    def transcript_language_list(self) -> list[str]:
        return self.transcript_languages.split(",")

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
    def gemini_unavailable_backoff_delays(self) -> list[int]:
        """Backoff steps for Gemini 503/504; NOT added to the delay-queue set (reached by hops)."""
        return [int(p) for p in self.gemini_unavailable_backoff_sec.split(",")]

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
