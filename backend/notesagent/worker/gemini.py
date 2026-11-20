"""Gemini access for the SUMMARIZING stage (ADR-06, ADR-15): a thin async client over the official
``google-genai`` SDK plus the mapping of its exceptions to classified pipeline errors (ADR-10).

- One ``generate()`` call = ONE Gemini request; the stage takes a limiter permit before each call.
- The SDK's own retries are disabled (``HttpRetryOptions(attempts=1)``): retries happen only through the
  existing outbox retry system (ADR-11).
- The API key comes from ``GEMINI_API_KEY`` (``SecretStr``); it is never logged, never put in error
  messages, and upstream exception text is never kept.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from notesagent.common.config import Settings
from notesagent.common.errors import FatalStageError, PipelineError, Provider, TransientError
from notesagent.common.http_errors import classify_exception, classify_http

P = Provider.GEMINI
_BLOCKED_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}


@dataclass(frozen=True)
class GeminiReply:
    text: str | None
    prompt_tokens: int | None
    output_tokens: int | None
    finish_reason: str | None
    blocked: bool


def gemini_api_key(settings: Settings) -> str:
    key = settings.gemini_api_key.get_secret_value().strip() if settings.gemini_api_key else ""
    return key


class GeminiClient:
    """Real client. The SDK client is created lazily (first request) inside the worker's event loop."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    def __repr__(self) -> str:                                    # never reveal the key
        return f"GeminiClient(model={self.settings.gemini_model!r})"

    def _sdk(self):
        if self._client is None:
            from google import genai
            from google.genai import types
            self._client = genai.Client(
                api_key=gemini_api_key(self.settings),
                http_options=types.HttpOptions(timeout=int(self.settings.gemini_request_timeout_sec * 1000),
                                               retry_options=types.HttpRetryOptions(attempts=1)))
        return self._client

    async def generate(self, *, system: str, prompt: str, schema: dict[str, Any]) -> GeminiReply:
        from google.genai import types
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
            max_output_tokens=self.settings.gemini_max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=self.settings.gemini_thinking_level.upper()),
        )
        response = await self._sdk().aio.models.generate_content(model=self.settings.gemini_model, contents=prompt,
                                                                 config=config)
        usage = getattr(response, "usage_metadata", None)
        candidates = getattr(response, "candidates", None) or []
        finish = getattr(candidates[0], "finish_reason", None) if candidates else None
        finish_name = getattr(finish, "name", None) or (str(finish) if finish else None)
        feedback = getattr(response, "prompt_feedback", None)
        blocked = bool(getattr(feedback, "block_reason", None)) or (finish_name in _BLOCKED_FINISH)
        try:
            text = response.text
        except Exception:  # noqa: BLE001 - e.g. no text part
            text = None
        return GeminiReply(text=text, prompt_tokens=getattr(usage, "prompt_token_count", None),
                           output_tokens=getattr(usage, "candidates_token_count", None),
                           finish_reason=finish_name, blocked=blocked)


def _plain_headers(headers: Any) -> dict[str, str] | None:
    items = getattr(headers, "items", None)
    if not callable(items):
        return None
    try:
        return {str(k): str(v) for k, v in items()}
    except (TypeError, ValueError):
        return None


def classify_gemini_error(exc: BaseException) -> PipelineError:
    """Map a google-genai / transport exception to a classified error (never keeps its text)."""
    if isinstance(exc, PipelineError):
        return exc
    try:
        from google.genai import errors as genai_errors
    except ImportError:                                                     # pragma: no cover
        genai_errors = None
    if genai_errors is not None and isinstance(exc, genai_errors.APIError):
        body = exc.details if isinstance(exc.details, dict) else {}
        document = body if "error" in body else {"error": body}
        headers = _plain_headers(getattr(getattr(exc, "response", None), "headers", None))
        classified = classify_http(P, exc.code, headers, document)
        if classified is not None:
            return classified
        return TransientError("GEMINI_UNEXPECTED_RESPONSE", "Gemini returned an unexpected response.", provider=P)
    try:
        import httpx2
        transport = (httpx2.TransportError,)
    except ImportError:                                                     # pragma: no cover
        transport = ()
    if transport and isinstance(exc, transport):
        return TransientError("GEMINI_UNREACHABLE", "Gemini could not be reached.", provider=P,
                              detail={"exception_type": type(exc).__name__})
    return classify_exception(P, exc)


def not_configured() -> FatalStageError:
    return FatalStageError("GEMINI_NOT_CONFIGURED", "GEMINI_API_KEY is not configured for the worker.", provider=P)
