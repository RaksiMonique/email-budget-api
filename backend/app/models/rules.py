from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MerchantRule(Base):
    __tablename__ = "merchant_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    pattern: Mapped[str] = mapped_column(String(255))
    match_type: Mapped[str] = mapped_column(String(16))  # starts_with|contains|exact|regex
    normalized_name: Mapped[str] = mapped_column(String(255))
    category: Mapped[str | None] = mapped_column(String(100))
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower wins
    confirmation_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CategoryFeedbackLog(Base):
    __tablename__ = "category_feedback_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    merchant_normalized: Mapped[str] = mapped_column(String(255), index=True)
    category_confirmed: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
