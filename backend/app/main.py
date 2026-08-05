"""FastAPI app factory."""
from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.internal import router as internal_router
from app.api.v1.aliases import router as aliases_router
from app.api.v1.config import router as config_router
from app.api.v1.extractions import router as extractions_router
from app.api.v1.feedback import router as feedback_router
from app.config import get_settings


_SENSITIVE_HEADERS = {"x-api-key", "x-internal-secret", "authorization", "cookie"}


def _scrub_sentry_event(event: dict, hint: dict) -> dict:
    """Strip auth material from Sentry events — custom auth headers are NOT in
    Sentry's default denylist and would otherwise ship with every 500."""
    headers = (event.get("request") or {}).get("headers")
    if isinstance(headers, dict):
        for name in list(headers):
            if name.lower() in _SENSITIVE_HEADERS:
                headers[name] = "[Filtered]"
    return event


def create_app() -> FastAPI:
    settings = get_settings()

    if settings.sentry_dsn:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            send_default_pii=False,
            before_send=_scrub_sentry_event,
            # never capture stack-frame locals: poller exceptions would ship
            # the DECRYPTED webhook secret held in process_due's locals
            include_local_variables=False,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Phase 7 outbox poller — delivery state lives in Postgres, so this
        # loop is stateless and redeploy-safe. Off in tests (process_due is
        # called directly there).
        task = None
        if settings.enable_outbox_poller:
            from app.services.webhook_delivery_service import poller_loop

            task = asyncio.create_task(poller_loop())
        yield
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    prod = settings.environment == "production"
    app = FastAPI(
        title="Email Budget API",
        lifespan=lifespan,
        # no unauthenticated API-surface disclosure in production:
        docs_url=None if prod else "/docs",
        redoc_url=None if prod else "/redoc",
        openapi_url=None if prod else "/openapi.json",
    )
    app.include_router(aliases_router)
    app.include_router(extractions_router)
    app.include_router(feedback_router)
    app.include_router(config_router)
    app.include_router(internal_router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
