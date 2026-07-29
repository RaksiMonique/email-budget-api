"""All ORM models — importing this module registers every table on Base.metadata
(required for Alembic autogenerate)."""
from app.models.alias import Alias
from app.models.email import EmailClassification, ImportAuditLog, ImportedEmail
from app.models.extraction import (
    DuplicateMatch,
    ExtractionResult,
    ExtractionSnippet,
    ExtractionTemplate,
)
from app.models.ops import ApiKey, WebhookConfig, WebhookOutbox
from app.models.rules import CategoryFeedbackLog, MerchantRule

__all__ = [
    "Alias",
    "ApiKey",
    "CategoryFeedbackLog",
    "DuplicateMatch",
    "EmailClassification",
    "ExtractionResult",
    "ExtractionSnippet",
    "ExtractionTemplate",
    "ImportAuditLog",
    "ImportedEmail",
    "MerchantRule",
    "WebhookConfig",
    "WebhookOutbox",
]
