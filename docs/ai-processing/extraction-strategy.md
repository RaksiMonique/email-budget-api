# Extraction Strategy

> Updated 2026-05-17. Heuristics-first approach. AI is deferred to Phase 2 and used only as a fallback for unknown senders, never for correction or "improvement" of partial results.

## Core Philosophy

**Wrong financial data is worse than no data.**

A hallucinated merchant name or incorrect amount erodes user trust immediately and permanently. The system's primary job is to be right when it is confident, and honest when it is not.

This means:
- Template extraction for known senders → very high accuracy (98%+)
- General regex for unknown senders → acceptable accuracy for standard formats
- **If neither produces confident results → mark as failed, surface to user**
- Never guess. Never fill in missing fields with heuristic approximations.

---

## Three-Stage Pipeline (MVP)

```
Stage 1: Sender Template
  Known sender domain → sender-specific regex patterns
  If all required fields extracted with high confidence → done

Stage 2: General Regex
  Standard amount / date / card patterns that work across most emails
  Partial results are acceptable but flagged

Stage 3: Failure
  Required field missing (amount or transaction_date) → extraction_failed
  Budget app notified: "couldn't parse this email"

[Phase 2 only]
Stage 3: AI Fallback (Claude Haiku)
  Only for emails where Stage 1 AND Stage 2 produce no result at all
  Strict confidence gating: < 0.85 confidence → treat as failed anyway
```

---

## Stage 1: Sender Templates

### What it is

Per-sender regex patterns matched against email body content. Each template is tuned to a specific sender's email format.

### Template structure

```python
@dataclass
class ExtractionTemplate:
    sender_domain: str
    email_type: str           # merchant_receipt, bank_alert, etc.
    
    # Patterns (all are regex strings with a single capture group)
    amount_pattern: str
    merchant_pattern: Optional[str]   # if None, use sender display name
    date_pattern: str
    currency: Optional[str]           # hardcoded if sender always uses same currency
    card_pattern: Optional[str]
    payment_method: Optional[str]     # hardcoded if always same (e.g. "PayPal")
    transaction_type: str             # hardcoded: debit / credit / refund
    
    # Reliability metadata
    version: int = 1
    success_count: int = 0
    failure_count: int = 0
```

### Example: Amazon receipt

```python
ExtractionTemplate(
    sender_domain="amazon.com",
    email_type="merchant_receipt",
    transaction_type="debit",
    merchant_pattern=None,           # merchant = "Amazon" (hardcoded from sender)
    amount_pattern=r"Order Total[:\s]*\$?([\d,]+\.\d{2})",
    date_pattern=r"Order Placed[:\s]+([A-Za-z]+ \d{1,2},? \d{4})",
    card_pattern=r"ending in (\d{4})",
    currency="USD",
)
```

### Example: Chase bank alert

```python
ExtractionTemplate(
    sender_domain="chase.com",
    email_type="bank_alert",
    transaction_type="debit",
    merchant_pattern=r"at\s+(.+?)\s+for",          # "at Amazon for"
    amount_pattern=r"\$\s*([\d,]+\.\d{2})",
    date_pattern=r"on\s+([A-Za-z]+ \d{1,2},? \d{4})",
    card_pattern=r"(?:card|account) ending in (\d{4})",
    currency="USD",
)
```

### Example: Stripe invoice

```python
ExtractionTemplate(
    sender_domain="stripe.com",
    email_type="subscription_invoice",
    transaction_type="debit",
    merchant_pattern=r"Invoice from\s+(.+)\n",
    amount_pattern=r"Amount due[:\s]+\$?([\d,]+\.\d{2})",
    date_pattern=r"Due\s+([A-Za-z]+ \d{1,2},? \d{4})",
    currency="USD",
)
```

### Template execution

```python
def apply_template(
    template: ExtractionTemplate,
    text_body: str,
    html_body: Optional[str],
) -> TemplateResult:
    # Use text body first; fall back to HTML→text if text is empty
    content = text_body or html_to_text(html_body or "")
    
    results = {}
    
    for field, pattern in template.field_patterns.items():
        if pattern is None:
            continue
        match = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
        if match:
            results[field] = match.group(1).strip()
    
    # Apply hardcoded fields from template
    if template.currency:
        results["currency"] = template.currency
    if template.transaction_type:
        results["transaction_type"] = template.transaction_type
    if template.merchant_pattern is None:
        results["merchant"] = template.display_merchant  # e.g., "Amazon"
    
    return TemplateResult(
        fields=results,
        template_id=template.id,
        confidence=_score_template_result(results, template),
    )

def _score_template_result(results: dict, template: ExtractionTemplate) -> float:
    required = {"amount", "transaction_date"}
    found_required = required.intersection(results.keys())
    
    if len(found_required) < len(required):
        return 0.0   # missing required field — template failed
    
    optional = {"merchant", "card_suffix", "currency", "payment_method"}
    found_optional = optional.intersection(results.keys())
    optional_score = len(found_optional) / len(optional)
    
    # Templates are highly reliable — base confidence is high
    return 0.90 + (optional_score * 0.09)  # 0.90 to 0.99
```

---

## Stage 2: General Regex Extraction

Runs when no sender template exists, or when the template fails to find required fields.

### Pattern library

```python
AMOUNT_PATTERNS = [
    # Dollar amounts
    r"\$\s*([\d,]+\.\d{2})",               # $45.99
    r"([\d,]+\.\d{2})\s*USD",              # 45.99 USD
    r"Total[:\s]+\$?([\d,]+\.\d{2})",      # Total: $45.99
    r"Amount[:\s]+\$?([\d,]+\.\d{2})",     # Amount: $45.99
    r"charged[:\s]+\$?([\d,]+\.\d{2})",    # charged: $45.99
    r"paid[:\s]+\$?([\d,]+\.\d{2})",       # paid: $45.99
    # European formats
    r"([\d.]+,\d{2})\s*(EUR|GBP|CAD|AUD)", # 1.234,56 EUR
    r"([\d,]+\.\d{2})\s*(EUR|GBP|CAD|AUD)",# 45.99 GBP
]

DATE_PATTERNS = [
    r"(\d{4}-\d{2}-\d{2})",                # 2026-05-17 (ISO)
    r"(\d{1,2}/\d{1,2}/\d{4})",            # 05/17/2026
    r"(\d{1,2}-\d{1,2}-\d{4})",            # 05-17-2026
    r"([A-Za-z]+ \d{1,2},? \d{4})",        # May 17, 2026
    r"(\d{1,2} [A-Za-z]+ \d{4})",          # 17 May 2026
]

CARD_PATTERNS = [
    r"ending in (\d{4})",
    r"card[:\s]*\*{4}(\d{4})",
    r"x{4}(\d{4})",
    r"\*{4}\s*(\d{4})",
    r"account ending (\d{4})",
]

CURRENCY_PATTERNS = [
    r"\b(USD|EUR|GBP|CAD|AUD|JPY|CHF|NZD|SEK|NOK|DKK|SGD|HKD|MXN|BRL)\b",
]
```

### Execution and confidence

```python
def general_regex_extract(text: str) -> GeneralResult:
    fields = {}
    
    # Amount: try patterns in order, take first confident match
    for pattern in AMOUNT_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            raw_amount = match.group(1).replace(",", "")
            try:
                fields["amount"] = Decimal(raw_amount)
                fields["raw_amount_snippet"] = match.group(0)[:100]
                break
            except InvalidOperation:
                continue
    
    # Date: try patterns, parse with dateparser for normalization
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            parsed = dateparser.parse(match.group(1), settings={"PREFER_DAY_OF_MONTH": "first"})
            if parsed and 2020 <= parsed.year <= 2030:
                fields["transaction_date"] = parsed.date()
                break
    
    # Card suffix
    for pattern in CARD_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            fields["card_suffix"] = match.group(1)
            break
    
    # Currency
    match = re.search(CURRENCY_PATTERNS[0], text, re.IGNORECASE)
    if match:
        fields["currency"] = match.group(1).upper()
    else:
        fields["currency"] = "USD"  # default for unknown
    
    return GeneralResult(fields=fields, confidence=_score_general(fields))

def _score_general(fields: dict) -> float:
    has_amount = "amount" in fields
    has_date = "transaction_date" in fields
    
    if not has_amount:
        return 0.0   # required: amount
    
    base = 0.65 if (has_amount and has_date) else 0.45
    bonus = 0.05 if "card_suffix" in fields else 0.0
    return base + bonus
```

---

## Confidence Thresholds and Routing

| Source | Confidence | Status | Action |
|--------|-----------|--------|--------|
| Template, all fields | 0.94–0.99 | `pending_review` | Ready for budgeting app review |
| Template, partial | 0.90–0.93 | `pending_review` | Low-confidence badge in UI |
| General regex, amount+date | 0.65–0.79 | `pending_review` | Low-confidence badge |
| General regex, amount only | 0.45–0.64 | `pending_review` | Very low confidence badge |
| No amount found | 0.0 | `extraction_failed` | Webhook: extraction.failed |
| Required field missing | 0.0 | `extraction_failed` | Webhook: extraction.failed |

**Required fields:** `amount` and `transaction_date`. Missing either = extraction failed.

---

## Content Preparation

Before any pattern matching:

```python
def prepare_content(email: ParsedEmail) -> str:
    # Prefer plain text; convert HTML if no text
    if email.text_body and len(email.text_body.strip()) > 50:
        text = email.text_body
    elif email.html_body:
        text = html_to_text(email.html_body)
    else:
        return ""  # no extractable content
    
    # Strip email footers (unsubscribe blocks, legal boilerplate)
    text = strip_email_footers(text)
    
    # Normalize whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    
    # Cap at 8000 characters — financial details are always in the first portion
    return text[:8000]
```

---

## Merge: Template Takes Priority

```python
def merge_results(
    template: Optional[TemplateResult],
    general: Optional[GeneralResult],
) -> ExtractionResult:
    # Template fields win over general regex if template found them
    merged = {}
    
    if general:
        merged.update(general.fields)
    if template:
        merged.update(template.fields)   # template overwrites general
    
    final_confidence = max(
        template.confidence if template else 0.0,
        general.confidence if general else 0.0,
    )
    method = "template" if template and template.confidence >= 0.90 else "general_regex"
    
    return ExtractionResult(**merged, extraction_confidence=final_confidence, method=method)
```

The merge is additive: if template found the amount but not the card suffix, and general regex found the card suffix, the final result has both.

---

## Failure Handling

When extraction fails, the budgeting app receives:

```json
{
  "event": "extraction.failed",
  "data": {
    "extraction_id": "uuid",
    "external_user_id": "...",
    "failure_reason": "no_amount_found",  // no_amount_found | no_date_found | no_financial_content | non_financial
    "email": {
      "from": "billing@unknownco.io",
      "subject": "Your invoice #123",
      "received_at": "2026-05-17T09:00:00Z"
    },
    "preview_url": "/api/v1/extractions/{id}/preview"  // budgeting app can show raw email
  }
}
```

The budgeting app shows users: "We received an email from billing@unknownco.io but couldn't read it automatically — [Enter manually]."

This is better than a wrong auto-extraction.

---

## Template Health Monitoring

Track success/failure counts per template to detect format changes:

```python
# After extraction attempt:
if extraction_succeeded:
    template.success_count += 1
else:
    template.failure_count += 1

# Alert condition:
failure_rate = template.failure_count / (template.success_count + template.failure_count)
if failure_rate > 0.20 and template.success_count + template.failure_count > 50:
    log_warning(f"Template degraded: {template.sender_domain}, failure_rate={failure_rate:.0%}")
    # → Sentry alert, review template
```

Degraded templates indicate the sender changed their email format. This is a signal to update the template.

---

## Phase 2: AI Fallback (Not in MVP)

When Stage 1 and Stage 2 both produce no result (extraction_confidence = 0.0), Phase 2 adds a Claude Haiku call as a last resort.

**Strict rules for AI use:**
1. Only triggered when both template AND general regex produce zero extractable fields
2. If AI confidence < 0.85 → treat as failed (same as no AI)
3. AI result is validated through the same Pydantic model as template results
4. AI result is stored with `method = "ai"` for auditing
5. AI result with amount that differs from any regex-found amount is flagged for review

The point of the AI fallback is to handle genuinely unusual email formats (small local merchants, international senders, unusual templating). It is not a crutch for poor template coverage.

See [ai-processing/confidence-scoring.md](confidence-scoring.md) for scoring details.

---

*See [parsing/rules-engine.md](../parsing/rules-engine.md) for merchant normalization and category suggestion.*
*See [architecture/redesign-summary.md](../architecture/redesign-summary.md) for the decision to prefer heuristics over AI.*
