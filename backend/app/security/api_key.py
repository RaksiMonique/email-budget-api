"""X-API-Key validation for the budgeting-app-facing /api/v1/* routes."""
from __future__ import annotations

import hashlib

from fastapi import Depends, Header, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models import ApiKey


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def require_api_key(
    x_api_key: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> ApiKey:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing API key")
    # sha256 then indexed equality — the digest comparison in SQL is not
    # timing-sensitive because the attacker-controlled value is pre-hashed
    key = (
        await db.execute(select(ApiKey).where(ApiKey.key_hash == hash_key(x_api_key)))
    ).scalar_one_or_none()
    if key is None:
        raise HTTPException(status_code=401, detail="invalid API key")
    await db.execute(
        update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=func.now())
    )
    # commit the touch NOW: this dependency runs before any route work, and
    # read-only routes never commit — without this the UPDATE is silently
    # rolled back when the request session closes (and its row lock held).
    await db.commit()
    return key
