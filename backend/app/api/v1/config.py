"""Webhook configuration endpoints (budgeting app → this API)."""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models import WebhookConfig
from app.schemas.extractions import WebhookConfigBody
from app.security import crypto
from app.security.api_key import require_api_key
from app.services import webhook_delivery_service as delivery

router = APIRouter(
    prefix="/api/v1/config", dependencies=[Depends(require_api_key)], tags=["config"]
)


@router.post("/webhook")
async def set_webhook_config(
    body: WebhookConfigBody, db: AsyncSession = Depends(get_db)
) -> dict:
    # strict URL validation at config time — `^https?://` alone admits strings
    # like "http://[::1" that later raise httpx.InvalidURL inside the poller
    try:
        parsed = httpx.URL(body.webhook_url)
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise ValueError("invalid scheme or host")
    except Exception:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="webhook_url is not a valid URL")

    db.add(
        WebhookConfig(
            webhook_url=body.webhook_url,
            webhook_secret_encrypted=crypto.encrypt(body.webhook_secret),
        )
    )
    await db.commit()
    return {"configured": True, "webhook_url": body.webhook_url}


@router.post("/webhook/test")
async def test_webhook(db: AsyncSession = Depends(get_db)) -> dict:
    cfg = await delivery.get_config(db)
    if cfg is None:
        return {"delivered": False, "error": "no webhook configured"}
    url, secret = cfg

    import json
    import time

    body = json.dumps(
        {"event": "test", "data": {"message": "Email Budget API webhook test"}},
        separators=(",", ":"),
    ).encode()
    ts = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-EmailBudget-Timestamp": str(ts),
        "X-EmailBudget-Signature": delivery.sign(secret, ts, body),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, content=body, headers=headers)
        return {"delivered": resp.is_success, "status_code": resp.status_code}
    except httpx.HTTPError as exc:
        return {"delivered": False, "error": f"{type(exc).__name__}: {exc}"}
