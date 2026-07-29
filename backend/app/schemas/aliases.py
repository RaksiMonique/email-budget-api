from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AliasCreate(BaseModel):
    external_user_id: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)


class AliasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    alias_hash: str
    email_address: str
    external_user_id: str
    label: str | None
    is_active: bool
    emails_received: int
    created_at: datetime
