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
from app.api.v1.users import router as users_router
from app.config import get_settings

_MAINTENANCE_INTERVAL_SECONDS = 3600


async def _maintenance_loop() -> None:
    """Hourly privacy sweep: purge grace-expired deletions + retention (Phase 8)."""
    import logging

    from app.db.session import async_session
    from app.services import privacy_service

    log = logging.getLogger(__name__)
    while True:
        try:
            async with async_session() as db:
                purged = await privacy_service.purge_due(db)
            async with async_session() as db:
                swept = await privacy_service.retention_sweep(db)
            if purged or swept:
                log.info("maintenance: purged=%s retention_swept=%s", purged, swept)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("maintenance loop iteration failed")
        await asyncio.sleep(_MAINTENANCE_INTERVAL_SECONDS)


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
            # never attach request bodies: the /internal payload carries the
            # sender address + subject (financial PII); a 500 must not ship them
            max_request_body_size="never",
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Background loops — all state lives in Postgres, so both are
        # stateless and redeploy-safe. Off in tests (functions called directly).
        tasks = []
        if settings.enable_outbox_poller:
            from app.services.webhook_delivery_service import poller_loop

            tasks.append(asyncio.create_task(poller_loop()))
        if settings.enable_maintenance_loop:
            tasks.append(asyncio.create_task(_maintenance_loop()))
        yield
        for task in tasks:
            task.cancel()
        for task in tasks:
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
    app.include_router(users_router)
    app.include_router(internal_router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
