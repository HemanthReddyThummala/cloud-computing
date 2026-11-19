"""FastAPI application factory (``uvicorn notesagent.gateway.main:create_app --factory``)."""
from __future__ import annotations

import contextlib
import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from notesagent.common.broker import BrokerClient
from notesagent.common.config import Settings, get_settings
from notesagent.common.crypto import CredentialCipher
from notesagent.common.db import open_pool
from notesagent.common.errors import ApiError
from notesagent.common.logging import setup_logging
from notesagent.gateway.routes import router
from notesagent.gateway.submission import JobSubmissionService

log = logging.getLogger(__name__)

_HTTP_CODES = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.service_name, settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start even if a dependency is down: /healthz then reports it honestly.
        pool = await open_pool(settings, name="gateway", min_size=1, max_size=10, wait=False)
        broker = BrokerClient(settings, name="gateway")
        await broker.start(wait=False)
        redis_client = aioredis.from_url(settings.redis_url.get_secret_value(),
                                         socket_timeout=2, socket_connect_timeout=2)
        cipher = CredentialCipher(settings.job_payload_encryption_key.get_secret_value()
                                  if settings.job_payload_encryption_key else "")
        app.state.settings = settings
        app.state.pool = pool
        app.state.broker = broker
        app.state.redis = redis_client
        app.state.submissions = JobSubmissionService(pool, broker, cipher, settings)
        log.info("gateway started")
        try:
            yield
        finally:
            await broker.close()
            await pool.close()
            with contextlib.suppress(Exception):
                await redis_client.aclose()

    app = FastAPI(title="Video Notes Agent — API Gateway", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _error(exc.status_code, _HTTP_CODES.get(exc.status_code, "HTTP_ERROR"),
                      "The requested resource or method is not available." if exc.status_code in _HTTP_CODES
                      else "The request could not be processed.")

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error(400, "INVALID_REQUEST", "Request parameters are invalid.")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error", extra={"error_type": type(exc).__name__})
        return _error(500, "INTERNAL_ERROR", "An unexpected error occurred.")

    app.include_router(router)
    return app
