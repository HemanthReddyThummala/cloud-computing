"""Notion OAuth (authorization-code flow) in the gateway — ADR-07, ADR-18.

Redirect model (ADR-07 "Mode A", the URI registered in the Notion integration): Notion redirects the
browser to the EXTENSION (``https://<extension-id>.chromiumapp.org/notion``, intercepted by Chrome's
``launchWebAuthFlow``). The extension then forwards ``code`` + ``state`` to the gateway:

1. ``POST /oauth/notion/authorize`` -> a fresh, unpredictable ``state`` (``secrets.token_urlsafe(32)``)
   is stored in Redis (only its SHA-256, single-use, TTL ``OAUTH_STATE_TTL_SEC``) and the Notion
   authorization URL is returned.
2. The extension opens that URL; the user picks pages and allows access; Notion redirects to the
   extension with ``code`` + ``state`` (or ``error``).
3. ``POST /oauth/notion/callback`` with ``{state, code}`` or ``{state, error}`` -> the state is CONSUMED
   (GETDEL: single use, also on errors); the code is exchanged server-side at Notion's token endpoint
   (HTTP Basic with the client ID/secret, ``redirect_uri`` = NOTION_REDIRECT_URI); the access token is
   returned only SEALED (``nva1.…``, ``TokenSealer``). The extension stores that and sends it as
   ``notion_token`` with each job; the gateway unseals it into the per-job mailbox (ADR-03).

No refresh: Notion's docs do not state that access tokens expire. Never logged or returned: the client
secret, authorization codes, raw access/refresh tokens. Upstream error bodies are never echoed.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from notesagent.common.errors import ApiError, Provider, RateLimited, RateLimiterUnavailable

log = logging.getLogger(__name__)
router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_STATE_PREFIX = "oauth:notion:state:"


class CallbackBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state: str = Field(min_length=1, max_length=256)
    code: str | None = Field(default=None, max_length=2048)
    error: str | None = Field(default=None, max_length=256)


def _state_key(state: str) -> str:
    return _STATE_PREFIX + hashlib.sha256(state.encode()).hexdigest()


def _configured(request: Request):
    settings = request.app.state.settings
    if not settings.notion_oauth_configured or request.app.state.sealer is None:
        raise ApiError(503, "OAUTH_NOT_CONFIGURED", "Notion sign-in is not configured on this gateway.",
                       headers=_NO_STORE)
    return settings


def authorization_url(settings, state: str) -> str:
    query = urlencode({"client_id": settings.notion_client_id, "response_type": "code", "owner": "user",
                       "redirect_uri": settings.notion_redirect_uri, "state": state})
    return f"{settings.notion_api_base_url.rstrip('/')}/oauth/authorize?{query}"


@router.post("/oauth/notion/authorize")
async def start_authorization(request: Request) -> JSONResponse:
    settings = _configured(request)
    state = secrets.token_urlsafe(32)
    try:
        stored = await request.app.state.redis.set(_state_key(state), "1", ex=settings.oauth_state_ttl_sec, nx=True)
    except Exception as exc:  # noqa: BLE001 - Redis unavailable
        log.warning("oauth state could not be stored", extra={"error_type": type(exc).__name__})
        raise ApiError(503, "OAUTH_STATE_STORE_UNAVAILABLE", "Sign-in is temporarily unavailable. Please retry.",
                       headers=_NO_STORE) from None
    if not stored:                                                       # practically impossible collision
        raise ApiError(503, "OAUTH_STATE_STORE_UNAVAILABLE", "Sign-in is temporarily unavailable. Please retry.",
                       headers=_NO_STORE)
    log.info("notion authorization started")
    return JSONResponse({"authorization_url": authorization_url(settings, state), "state": state,
                         "redirect_uri": settings.notion_redirect_uri,
                         "expires_in": settings.oauth_state_ttl_sec}, headers=_NO_STORE)


async def _consume_state(request: Request, state: str) -> bool:
    try:
        return await request.app.state.redis.getdel(_state_key(state)) is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("oauth state could not be checked", extra={"error_type": type(exc).__name__})
        raise ApiError(503, "OAUTH_STATE_STORE_UNAVAILABLE", "Sign-in is temporarily unavailable. Please retry.",
                       headers=_NO_STORE) from None


async def _exchange(request: Request, settings, code: str) -> dict[str, Any]:
    limiter = request.app.state.limiter
    try:                                                                  # shared Notion limit (ADR-13)
        await limiter.acquire(Provider.NOTION, connection="oauth-token-exchange", max_wait_sec=5)
    except RateLimited as exc:
        raise ApiError(503, "NOTION_RATE_LIMITED", "Notion is busy. Please try again shortly.",
                       headers={**_NO_STORE, "Retry-After": str(int(exc.retry_after_sec or 5) + 1)}) from None
    except RateLimiterUnavailable:
        raise ApiError(503, "OAUTH_STATE_STORE_UNAVAILABLE", "Sign-in is temporarily unavailable. Please retry.",
                       headers=_NO_STORE) from None
    url = f"{settings.notion_api_base_url.rstrip('/')}/oauth/token"
    try:
        async with httpx.AsyncClient(timeout=15, transport=request.app.state.oauth_transport) as client:
            response = await client.post(
                url, auth=(settings.notion_client_id, settings.notion_client_secret.get_secret_value()),
                headers={"Notion-Version": settings.notion_version},
                json={"grant_type": "authorization_code", "code": code, "redirect_uri": settings.notion_redirect_uri})
    except httpx.HTTPError as exc:
        log.warning("notion token exchange unreachable", extra={"error_type": type(exc).__name__})
        raise ApiError(502, "NOTION_UNREACHABLE", "Notion could not be reached. Please try again.",
                       headers=_NO_STORE) from None
    status = response.status_code
    if status == 200:
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get("access_token"), str) and data["access_token"]:
            return data
        log.warning("notion token exchange returned no access token")
        raise ApiError(502, "OAUTH_EXCHANGE_FAILED", "Notion did not return an access token. Please try again.",
                       headers=_NO_STORE)
    log.warning("notion token exchange rejected", extra={"http_status": status})
    if status == 429:
        raise ApiError(503, "NOTION_RATE_LIMITED", "Notion is busy. Please try again shortly.",
                       headers={**_NO_STORE, "Retry-After": response.headers.get("retry-after", "5")})
    if status in (401, 403):
        raise ApiError(502, "OAUTH_CLIENT_REJECTED",
                       "Notion rejected this gateway's integration credentials (check NOTION_CLIENT_ID/SECRET).",
                       headers=_NO_STORE)
    if 400 <= status < 500:
        raise ApiError(400, "OAUTH_EXCHANGE_FAILED",
                       "The authorization could not be completed (code invalid, expired or already used). "
                       "Please connect Notion again.", headers=_NO_STORE)
    raise ApiError(502, "NOTION_UNAVAILABLE", "Notion is temporarily unavailable. Please try again.",
                   headers=_NO_STORE)


@router.post("/oauth/notion/callback")
async def finish_authorization(request: Request) -> JSONResponse:
    settings = _configured(request)
    try:
        body = CallbackBody.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise ApiError(400, "OAUTH_INVALID_REQUEST", "Expected a JSON object with state and code (or error).",
                       headers=_NO_STORE) from None
    if not await _consume_state(request, body.state):                     # single use, also on errors
        raise ApiError(400, "OAUTH_STATE_INVALID", "The sign-in attempt is unknown, expired or already used. "
                                                   "Please connect Notion again.", headers=_NO_STORE)
    if body.error:
        log.info("notion authorization not granted", extra={"reason": "access_denied" if body.error ==
                                                             "access_denied" else "provider_error"})
        if body.error == "access_denied":
            raise ApiError(400, "OAUTH_ACCESS_DENIED", "Notion access was not granted.", headers=_NO_STORE)
        raise ApiError(400, "OAUTH_AUTHORIZATION_FAILED", "Notion reported an authorization error.",
                       headers=_NO_STORE)
    if not body.code:
        raise ApiError(400, "OAUTH_CODE_MISSING", "The authorization code is missing.", headers=_NO_STORE)

    data = await _exchange(request, settings, body.code)
    sealed = request.app.state.sealer.seal(data["access_token"], data.get("workspace_id"))
    log.info("notion authorization completed")
    return JSONResponse({"notion_token": sealed, "token_format": "sealed-v1",
                         "workspace_id": data.get("workspace_id"), "workspace_name": data.get("workspace_name"),
                         "bot_id": data.get("bot_id")}, headers=_NO_STORE)
