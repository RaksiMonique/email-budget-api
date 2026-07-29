from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Alias(Base):
    __tablename__ = "aliases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # stored lowercase — the Email Worker lowercases recipients (email local
    # parts are case-insensitive in practice)
    alias_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    external_user_id: Mapped[str] = mapped_column(String(255), index=True)
    label: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # increments on EVERY accepted email regardless of classification —
    # the onboarding "waiting for first email" poll target
    emails_received: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
