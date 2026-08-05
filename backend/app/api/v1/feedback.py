"""Category feedback + extraction stats (budgeting app → this API)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models import (
    CategoryFeedbackLog,
    ExtractionResult,
    ImportedEmail,
    MerchantRule,
)
from app.schemas.extractions import CategoryFeedbackBody
from app.security.api_key import require_api_key

router = APIRouter(
    prefix="/api/v1", dependencies=[Depends(require_api_key)], tags=["feedback"]
)

RULE_THRESHOLD = 3  # same merchant+category confirmed this many times → rule


@router.post("/feedback/category")
async def category_feedback(
    body: CategoryFeedbackBody, db: AsyncSession = Depends(get_db)
) -> dict:
    db.add(
        CategoryFeedbackLog(
            extraction_id=body.extraction_id,
            merchant_normalized=body.merchant_normalized,
            category_confirmed=body.category_confirmed,
        )
    )
    await db.flush()

    confirmations = (
        await db.execute(
            select(func.count())
            .select_from(CategoryFeedbackLog)
            .where(
                CategoryFeedbackLog.merchant_normalized == body.merchant_normalized,
                CategoryFeedbackLog.category_confirmed == body.category_confirmed,
            )
        )
    ).scalar_one()

    rule_created = False
    if confirmations >= RULE_THRESHOLD:
        pattern = body.merchant_normalized.upper()
        rule = (
            await db.execute(
                select(MerchantRule).where(
                    MerchantRule.pattern == pattern, MerchantRule.match_type == "exact"
                )
            )
        ).scalar_one_or_none()
        if rule is None:
            db.add(
                MerchantRule(
                    pattern=pattern,
                    match_type="exact",
                    normalized_name=body.merchant_normalized,
                    category=body.category_confirmed,
                    confirmation_count=confirmations,
                    priority=50,  # feedback-learned rules outrank seeds (lower wins)
                )
            )
            rule_created = True
        else:
            rule.category = body.category_confirmed
            rule.confirmation_count = confirmations

    await db.commit()
    return {
        "recorded": True,
        "confirmations": confirmations,
        "rule_created_or_updated": confirmations >= RULE_THRESHOLD,
        "rule_created": rule_created,
    }


@router.get("/stats/extraction")
async def extraction_stats(external_user_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    by_status = {
        status: count
        for status, count in (
            await db.execute(
                select(ExtractionResult.status, func.count())
                .where(ExtractionResult.external_user_id == external_user_id)
                .group_by(ExtractionResult.status)
            )
        ).all()
    }
    total = sum(by_status.values())
    ok = sum(by_status.get(s, 0) for s in ("pending_review", "confirmed"))

    top_senders = [
        {"sender": s, "count": c}
        for s, c in (
            await db.execute(
                select(ImportedEmail.resolved_sender_domain, func.count())
                .join(ExtractionResult, ExtractionResult.email_id == ImportedEmail.id)
                .where(
                    ExtractionResult.external_user_id == external_user_id,
                    ImportedEmail.resolved_sender_domain.is_not(None),
                )
                .group_by(ImportedEmail.resolved_sender_domain)
                .order_by(func.count().desc())
                .limit(5)
            )
        ).all()
    ]
    top_failing = [
        {"sender": s, "count": c}
        for s, c in (
            await db.execute(
                select(ImportedEmail.resolved_sender_domain, func.count())
                .join(ExtractionResult, ExtractionResult.email_id == ImportedEmail.id)
                .where(
                    ExtractionResult.external_user_id == external_user_id,
                    ExtractionResult.status == "extraction_failed",
                    ImportedEmail.resolved_sender_domain.is_not(None),
                )
                .group_by(ImportedEmail.resolved_sender_domain)
                .order_by(func.count().desc())
                .limit(5)
            )
        ).all()
    ]

    return {
        "external_user_id": external_user_id,
        "total_extractions": total,
        "by_status": by_status,
        "success_rate": round(ok / total, 3) if total else None,
        "top_senders": top_senders,
        "top_failing_senders": top_failing,
    }
