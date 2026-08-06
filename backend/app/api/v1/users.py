"""User data privacy endpoints (budgeting app → this API)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.security.api_key import require_api_key
from app.services import privacy_service

router = APIRouter(
    prefix="/api/v1/users", dependencies=[Depends(require_api_key)], tags=["privacy"]
)


@router.delete("/{external_user_id}/data")
async def delete_user_data(
    external_user_id: str, db: AsyncSession = Depends(get_db)
) -> dict:
    """Deactivate the user's aliases and schedule raw-email deletion after the
    grace period. Idempotent — repeat calls schedule nothing new."""
    return await privacy_service.schedule_user_deletion(db, external_user_id)


@router.get("/{external_user_id}/data-export")
async def export_user_data(external_user_id: str) -> dict:
    """Stub — full GDPR export (JSON + CSV packaging) ships in Phase 2."""
    raise HTTPException(
        status_code=501,
        detail="data export is not implemented yet (planned: Phase 2); "
        "extraction data is queryable via GET /api/v1/extractions in the meantime",
    )
