from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EmailReceivedPayload(BaseModel):
    """Queue message shape produced by the Email Worker."""

    model_config = ConfigDict(populate_by_name=True)

    email_id: str
    alias_hash: str
    r2_key: str
    from_address: str = Field(alias="from", default="")
    to: str = ""
    subject: str = ""
    message_id: str = ""
    date_header: str = ""
    received_at: str = ""
