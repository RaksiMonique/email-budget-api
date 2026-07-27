# Confidence Scoring

## Overview

Two confidence scores are computed for each extraction:
1. **`extraction_confidence`**: How reliably were the transaction fields extracted?
2. **`duplicate_confidence`**: How likely is this a duplicate of an existing transaction?

Both are floats in `[0.0, 1.0]`. Scores drive pipeline routing and UI presentation.

---

## Extraction Confidence

### How It's Computed

Extraction confidence is a **weighted average of per-field confidence scores**, adjusted by method and validation outcomes.

#### Per-Field Confidence

| Field | Required | Weight |
|-------|----------|--------|
| `amount` | Yes | 0.35 |
| `merchant` | Yes | 0.25 |
| `transaction_date` | Yes | 0.20 |
| `currency` | Yes | 0.10 |
| `transaction_type` | No | 0.05 |
| `card_suffix` | No | 0.03 |
| `payment_method` | No | 0.02 |

#### Per-Field Score

```python
def score_field(field, value, method, email_context) -> float:
    if value is None:
        return 0.0
    
    if method == "template":
        return 0.97    # template match is highly reliable
    
    if method == "rule":
        validators = {
            "amount":   validate_amount_format,
            "date":     validate_date_plausible,
            "currency": validate_iso_currency,
        }
        if field in validators:
            return 0.80 if validators[field](value) else 0.40
        return 0.70
    
    if method == "ai":
        # Use AI's self-reported confidence as starting point
        base = ai_result.extraction_confidence
        # Penalize if AI value contradicts regex-found value
        if regex_found_value and not values_match(value, regex_found_value):
            base *= 0.60
        return base
```

#### Overall Score Formula

```python
def compute_extraction_confidence(
    field_scores: dict[str, float],
    weights: dict[str, float],
    method: str,
    validation_passed: bool,
) -> float:
    # Weighted average of required fields
    required_score = sum(
        field_scores[f] * weights[f]
        for f in ["amount", "merchant", "transaction_date", "currency"]
        if f in field_scores
    )
    # Optional fields bonus (up to 0.05 bonus)
    optional_score = sum(
        field_scores.get(f, 0) * weights[f]
        for f in ["transaction_type", "card_suffix", "payment_method"]
    )
    
    base = required_score + optional_score
    
    # Penalize if Pydantic validation failed on any field
    if not validation_passed:
        base *= 0.70
    
    # Cap at 0.99 for AI extraction (no extraction is perfect)
    if method == "ai":
        base = min(base, 0.99)
    
    return round(base, 3)
```

### Score Thresholds and Routing

| Score | Status | Action |
|-------|--------|--------|
| `≥ 0.90` | High confidence | → `pending_review` (normal queue) |
| `0.70 – 0.89` | Medium confidence | → `pending_review` with yellow confidence badge |
| `0.50 – 0.69` | Low confidence | → `needs_review` with orange badge, extraction details shown |
| `< 0.50` | Very low | → `needs_review` or `extraction_failed` depending on missing fields |
| Required field missing | Incomplete | → `extraction_failed` if amount or date missing |

### Auto-Approve Threshold

If user has configured `auto_approve_threshold` (e.g., 0.95):
- Transactions above threshold are auto-approved
- Budget app webhook fires immediately
- User still sees them in history but doesn't need to act

---

## Duplicate Confidence

### Why It's Separate

Duplicate confidence is computed after extraction confidence. A transaction can have high extraction confidence but also high duplicate confidence — both scores are shown to the user.

### How It's Computed

```python
def compute_duplicate_confidence(
    extraction: ExtractionResult,
    candidates: list[CandidateTransaction],
) -> float:
    if not candidates:
        return 0.0
    
    best_score = 0.0
    for candidate in candidates:
        score = compute_candidate_similarity(extraction, candidate)
        best_score = max(best_score, score)
    
    return round(best_score, 3)

def compute_candidate_similarity(a, b) -> float:
    scores = []
    
    # Amount similarity (exact match = 1.0, within 1% = 0.9)
    amount_diff = abs(a.amount - b.amount) / max(a.amount, b.amount)
    amount_score = max(0, 1.0 - (amount_diff * 10))
    scores.append(("amount", amount_score, 0.40))
    
    # Merchant similarity (trigram similarity via pg_trgm)
    merchant_score = trigram_similarity(
        normalize_merchant(a.merchant),
        normalize_merchant(b.merchant)
    )
    scores.append(("merchant", merchant_score, 0.35))
    
    # Date proximity (same day = 1.0, ±1 day = 0.7, ±3 days = 0.3)
    day_diff = abs((a.transaction_date - b.transaction_date).days)
    date_score = {0: 1.0, 1: 0.7, 2: 0.5, 3: 0.3}.get(day_diff, 0.0)
    scores.append(("date", date_score, 0.20))
    
    # Source bonus: different sources (forwarded vs inbox) increase duplicate probability
    source_bonus = 0.05 if a.source != b.source else 0.0
    
    weighted = sum(score * weight for _, score, weight in scores) + source_bonus
    return min(weighted, 1.0)
```

### Duplicate Thresholds

| Duplicate Confidence | Behavior |
|---------------------|----------|
| `1.0` | Exact message_id match — auto-suppressed, never shown |
| `≥ 0.95` | Highly probable duplicate — auto-suppressed, `needs_review` if user wants to unblock |
| `0.60 – 0.94` | Probable duplicate — shown in queue with "Possible Duplicate" badge, duplicate match visible |
| `0.30 – 0.59` | Possible duplicate — minor badge, shown in detail view only |
| `< 0.30` | No duplicate flag |

---

## UI Score Presentation

```
┌────────────────────────────────────────────────────┐
│ Amazon  •  $45.99  •  May 6, 2026                 │
│ Visa ending 1234  •  Shopping                      │
│                                                     │
│ Confidence: ████████░░  94%    ⚑ Possible Duplicate│
│             [Approve]  [Edit]  [Reject]             │
└────────────────────────────────────────────────────┘
```

- Green badge: ≥ 90%
- Yellow badge: 70–89%
- Orange badge: 50–69%
- Red badge: < 50%
- Duplicate warning: when duplicate_confidence ≥ 0.60

---

## Feedback Loop

User corrections improve confidence calibration over time:

1. When a user **edits an amount** before approving → record original vs corrected value
2. When a user **rejects** with reason "wrong amount" → decrement template confidence
3. When extraction confidence was 0.9+ and user edited → log mismatch for analysis
4. Monthly job: recalibrate field weights based on correction patterns

This data feeds:
- Template degradation detection (template was 0.97 but users correcting 30% of Amazon amounts → flag template for review)
- Accuracy metrics by sender, model, and method

---

*See [ai-processing/extraction-strategy.md](extraction-strategy.md) for extraction details.*
*See [duplicate-detection/duplicate-detection.md](../duplicate-detection/duplicate-detection.md) for duplicate detection algorithm.*
