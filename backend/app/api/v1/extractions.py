"""Extraction results API (budgeting app → this API, X-API-Key auth)."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.extraction.models import Status as PipelineStatus
from app.extraction.pipeline import run as run_pipeline
from app.integrations import r2_client
from app.services import duplicate_service
from app.models import (
    DuplicateMatch,
    ExtractionResult,
    ExtractionSnippet,
    ImportAuditLog,
    ImportedEmail,
)
from app.schemas.extractions import (
    ConfirmBody,
    DismissBody,
    DuplicateMatchOut,
    ExtractionDetail,
    ExtractionPage,
    ExtractionPreview,
    ExtractionSummary,
    SnippetOut,
)
from app.security.api_key import require_api_key
from app.services.extraction_service import apply_result_fields

router = APIRouter(
    prefix="/api/v1/extractions", dependencies=[Depends(require_api_key)], tags=["extractions"]
)


async def _get_row(db: AsyncSession, extraction_id: uuid.UUID) -> ExtractionResult:
    row = (
        await db.execute(select(ExtractionResult).where(ExtractionResult.id == extraction_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="extraction not found")
    return row


async def _detail(db: AsyncSession, row: ExtractionResult) -> ExtractionDetail:
    matches = (
        (
            await db.execute(
                select(DuplicateMatch).where(DuplicateMatch.extraction_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    detail = ExtractionDetail.model_validate(row)
    detail.duplicate_matches = [DuplicateMatchOut.model_validate(m) for m in matches]
    return detail


@router.get("", response_model=ExtractionPage)
async def list_extractions(
    external_user_id: str,
    status: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> ExtractionPage:
    conds = [ExtractionResult.external_user_id == external_user_id]
    if status:
        conds.append(ExtractionResult.status == status)
    if date_from:
        conds.append(ExtractionResult.created_at >= date_from)
    if date_to:
        conds.append(ExtractionResult.created_at < date_to)

    total = (
        await db.execute(select(func.count()).select_from(ExtractionResult).where(*conds))
    ).scalar_one()
    rows = (
        (
            await db.execute(
                select(ExtractionResult)
                .where(*conds)
                .order_by(ExtractionResult.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ExtractionPage(
        items=[ExtractionSummary.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{extraction_id}", response_model=ExtractionDetail)
async def get_extraction(
    extraction_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ExtractionDetail:
    return await _detail(db, await _get_row(db, extraction_id))


@router.get("/{extraction_id}/preview", response_model=ExtractionPreview)
async def preview_extraction(
    extraction_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ExtractionPreview:
    row = await _get_row(db, extraction_id)
    snippets = (
        (
            await db.execute(
                select(ExtractionSnippet).where(ExtractionSnippet.extraction_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    return ExtractionPreview(
        extraction=await _detail(db, row),
        snippets=[SnippetOut.model_validate(s) for s in snippets],
    )


def _audit(db: AsyncSession, row: ExtractionResult, action: str, new_status: str) -> None:
    db.add(
        ImportAuditLog(
            email_id=row.email_id,
            action=action,
            previous_status=row.status,
            new_status=new_status,
        )
    )


@router.post("/{extraction_id}/confirm", response_model=ExtractionDetail)
async def confirm_extraction(
    extraction_id: uuid.UUID,
    body: ConfirmBody,
    db: AsyncSession = Depends(get_db),
) -> ExtractionDetail:
    row = await _get_row(db, extraction_id)
    if row.status == "confirmed":  # idempotent — retries return the same result
        return await _detail(db, row)
    if row.status == "dismissed":
        raise HTTPException(status_code=409, detail="extraction was dismissed")
    _audit(db, row, "confirm", "confirmed")
    row.status = "confirmed"
    row.category_confirmed = body.category
    await db.commit()
    return await _detail(db, row)


@router.post("/{extraction_id}/dismiss", response_model=ExtractionDetail)
async def dismiss_extraction(
    extraction_id: uuid.UUID,
    body: DismissBody,
    db: AsyncSession = Depends(get_db),
) -> ExtractionDetail:
    row = await _get_row(db, extraction_id)
    if row.status == "dismissed":  # idempotent
        return await _detail(db, row)
    if row.status == "confirmed":
        raise HTTPException(status_code=409, detail="extraction was confirmed")
    _audit(db, row, "dismiss", "dismissed")
    row.status = "dismissed"
    row.dismissed_reason = body.reason
    await db.commit()
    return await _detail(db, row)


@router.post("/{extraction_id}/reprocess", response_model=ExtractionDetail)
async def reprocess_extraction(
    extraction_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ExtractionDetail:
    """Re-run the pipeline on the stored raw email (e.g. after a template
    improves) and update this row in place. User decisions are protected:
    confirmed/dismissed rows are not reprocessable."""
    row = await _get_row(db, extraction_id)
    if row.status in ("confirmed", "dismissed"):
        raise HTTPException(status_code=409, detail=f"extraction is {row.status}")

    email = (
        await db.execute(select(ImportedEmail).where(ImportedEmail.id == row.email_id))
    ).scalar_one_or_none()
    if email is None or not email.r2_key:
        raise HTTPException(status_code=409, detail="raw email no longer available")

    try:
        raw = await r2_client.get_object(email.r2_key)
    except r2_client.R2ObjectMissing:
        raise HTTPException(status_code=409, detail="raw email no longer available")

    result = run_pipeline(raw)
    # extraction rows hold ONLY extraction statuses — a reclassification
    # (non_financial / forwarding_verification) must not leak off-vocabulary
    # statuses into extraction_results (review finding, 2026-08-05)
    if result.status not in (PipelineStatus.PENDING_REVIEW, PipelineStatus.EXTRACTION_FAILED):
        raise HTTPException(
            status_code=409,
            detail=f"email no longer classifies as financial ({result.status.value}); "
            "dismiss this extraction instead",
        )

    _audit(db, row, "reprocess", result.status.value)
    apply_result_fields(row, result)  # same mapper as initial persistence
    email.status = "processed" if row.status == "pending_review" else "extraction_failed"

    # duplicate artifacts must reflect the NEW extraction, not the old one
    # (review finding: stale DuplicateMatch/confidence after reprocess)
    row.duplicate_confidence = Decimal("0")
    await db.execute(delete(DuplicateMatch).where(DuplicateMatch.extraction_id == row.id))
    await db.flush()
    await duplicate_service.check_and_flag(db, row)

    # snippets reflect the latest extraction
    await db.execute(delete(ExtractionSnippet).where(ExtractionSnippet.extraction_id == row.id))
    for field_name, field in result.fields.items():
        if field.snippet:
            db.add(
                ExtractionSnippet(
                    extraction_id=row.id, raw_snippet=field.snippet, snippet_type=field_name
                )
            )
    await db.commit()
    return await _detail(db, row)
