"""Pure, cloud-free extraction pipeline: raw .eml bytes -> ExtractionResult.

No R2, no DB, no network. This is the Phase 1 core; Phase 4 wires it behind the
FastAPI /internal webhook (fetching bytes from R2 and persisting). See PLAN.md Phase 1.
"""
from __future__ import annotations

import hashlib

from app.extraction import (
    confidence_scorer,
    content_preparer,
    general_extractor,
    mime_parser,
    sender_resolver,
    template_extractor,
    verification_detector,
)
from app.extraction.models import (
    Classification,
    EmailType,
    ExtractionResult,
    Field,
    Status,
)
from app.seed.financial_senders import DEFAULT_CURRENCY, SENDER_DEFAULT_CURRENCY
from app.services import classification_service, rules_engine


def run(raw: bytes) -> ExtractionResult:
    parsed = mime_parser.parse(raw)
    sender = sender_resolver.resolve(parsed)

    # Forwarding-verification mail (e.g. Gmail's confirm-your-forwarding-address)
    # is detected BEFORE financial classification — its sender resolves to
    # google.com, which the registry would misread as a Play-store receipt.
    if verification := verification_detector.detect(parsed):
        fields = {}
        if verification.code:
            fields["verification_code"] = Field(verification.code, "template")
        if verification.confirmation_url:
            fields["confirmation_url"] = Field(verification.confirmation_url, "template")
        return ExtractionResult(
            resolved_sender=sender,
            classification=Classification(
                False, EmailType.FORWARDING_VERIFICATION, 0.99, "verification_sender"
            ),
            fields=fields,
            merchant_normalized=None,
            category_suggestion=None,
            field_confidences={},
            extraction_confidence=0.0,
            confidence_band="n/a",
            status=Status.FORWARDING_VERIFICATION,
        )

    classification = classification_service.classify(sender, parsed.subject)

    if not classification.is_financial:
        return ExtractionResult(
            resolved_sender=sender,
            classification=classification,
            fields={},
            merchant_normalized=None,
            category_suggestion=None,
            field_confidences={},
            extraction_confidence=0.0,
            confidence_band="n/a",
            status=Status.NON_FINANCIAL,
        )

    content = content_preparer.prepare(parsed)

    # general first, then template overwrites on overlap (template is higher-confidence)
    fields: dict[str, Field] = {}
    fields.update(general_extractor.extract(content))
    fields.update(template_extractor.extract(sender.domain, content))

    # refine a generic classification using a matched template's declared type
    tmpl_type = template_extractor.email_type_for(sender.domain)
    if tmpl_type and classification.email_type == EmailType.UNKNOWN:
        classification = Classification(
            True, tmpl_type, classification.confidence, classification.method
        )

    # Fill an absent currency (bare "$" or none) from the sender's default
    # currency (JM banks → JMD) or the global default. Sender-aware, not a blind
    # guess; marked method="default" so it adds no confidence weight.
    if "amount" in fields and "currency" not in fields:
        ccy = SENDER_DEFAULT_CURRENCY.get(sender.domain or "") or DEFAULT_CURRENCY
        fields["currency"] = Field(ccy, "default")

    merchant_raw = fields["merchant"].value if "merchant" in fields else None
    normalized, category = rules_engine.normalize_merchant(merchant_raw)

    overall, conf = confidence_scorer.score(fields)
    status, band = confidence_scorer.route(overall, fields)

    # debit vs credit/refund + declined — a positive `amount` plus a direction,
    # so a refund reduces (not inflates) spending and a declined charge is flagged
    direction, is_probable_refund, is_declined = general_extractor.transaction_flags(content)

    return ExtractionResult(
        resolved_sender=sender,
        classification=classification,
        fields=fields,
        merchant_normalized=normalized,
        category_suggestion=category,
        field_confidences=conf,
        extraction_confidence=round(overall, 3),
        confidence_band=band,
        status=status,
        fingerprint=_fingerprint(fields, normalized),
        direction=direction,
        is_probable_refund=is_probable_refund,
        is_declined=is_declined,
    )


def _fingerprint(fields: dict[str, Field], merchant_normalized: str | None) -> str | None:
    amount = fields.get("amount")
    txn_date = fields.get("transaction_date")
    if not (amount and txn_date and merchant_normalized):
        return None
    minor_units = int((amount.value * 100).to_integral_value())
    currency = fields["currency"].value if "currency" in fields else "USD"
    basis = f"{minor_units}|{currency}|{merchant_normalized.lower()}|{txn_date.value.isoformat()}"
    return hashlib.sha256(basis.encode()).hexdigest()
