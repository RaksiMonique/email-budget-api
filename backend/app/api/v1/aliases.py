"""Alias management endpoints (budgeting app → this API, X-API-Key auth)."""
from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db
from app.models import Alias, ApiKey
from app.schemas.aliases import AliasCreate, AliasOut
from app.security.api_key import require_api_key

router = APIRouter(prefix="/api/v1/aliases", tags=["aliases"])


def _to_out(alias: Alias) -> AliasOut:
    return AliasOut(
        id=alias.id,
        alias_hash=alias.alias_hash,
        email_address=f"{alias.alias_hash}@{get_settings().email_domain}",
        external_user_id=alias.external_user_id,
        label=alias.label,
        is_active=alias.is_active,
        emails_received=alias.emails_received,
        created_at=alias.created_at,
    )


@router.post("", response_model=AliasOut, status_code=201)
async def create_alias(
    body: AliasCreate,
    db: AsyncSession = Depends(get_db),
    key: ApiKey = Depends(require_api_key),
) -> AliasOut:
    # lowercase: the Email Worker lowercases recipients (local parts are
    # case-insensitive in practice). ~77 bits of entropy after case-folding.
    for _attempt in range(5):
        token = secrets.token_urlsafe(12).lower()
        exists = (
            await db.execute(select(Alias.id).where(Alias.alias_hash == token))
        ).scalar_one_or_none()
        if exists is None:
            alias = Alias(
                alias_hash=token,
                external_user_id=body.external_user_id,
                api_key_id=key.id,  # routes this user's events to the caller's webhook
                label=body.label,
            )
            db.add(alias)
            await db.commit()
            await db.refresh(alias)
            return _to_out(alias)
    raise HTTPException(status_code=500, detail="could not generate unique alias")


@router.get("", response_model=list[AliasOut])
async def list_aliases(
    external_user_id: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_api_key),
) -> list[AliasOut]:
    rows = (
        (
            await db.execute(
                select(Alias)
                .where(Alias.external_user_id == external_user_id)
                .order_by(Alias.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [_to_out(a) for a in rows]


@router.get("/{alias_id}", response_model=AliasOut)
async def get_alias(
    alias_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_api_key),
) -> AliasOut:
    """Alias detail incl. emails_received — the onboarding 'waiting for first
    email' poll target (increments on every accepted email)."""
    alias = (
        await db.execute(select(Alias).where(Alias.id == alias_id))
    ).scalar_one_or_none()
    if alias is None:
        raise HTTPException(status_code=404, detail="alias not found")
    return _to_out(alias)


@router.delete("/{alias_id}", status_code=204)
async def deactivate_alias(
    alias_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_api_key),
) -> None:
    alias = (
        await db.execute(select(Alias).where(Alias.id == alias_id))
    ).scalar_one_or_none()
    if alias is None:
        raise HTTPException(status_code=404, detail="alias not found")
    alias.is_active = False
    await db.commit()
