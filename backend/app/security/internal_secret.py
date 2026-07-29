"""X-Internal-Secret validation for /internal/* routes (Cloudflare → FastAPI)."""
from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from app.config import get_settings


async def require_internal_secret(
    x_internal_secret: str = Header(default=""),
) -> None:
    expected = get_settings().internal_secret
    if not expected or not secrets.compare_digest(x_internal_secret, expected):
        raise HTTPException(status_code=403, detail="forbidden")
