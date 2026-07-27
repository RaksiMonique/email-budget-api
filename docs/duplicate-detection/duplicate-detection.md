# Duplicate Detection Strategy — J.

## Why Duplicates Occur

The most common source of duplicates in this system:

| Scenario | Example |
|----------|---------|
| Bank alert + merchant receipt | Chase sends "You spent $45.99 at Amazon" AND Amazon sends "Your order receipt" |
| Inbox scan + forwarded email | Email imported via inbox scan AND user also forwarded the same email |
| Inbox webhook + scheduled scan *(Phase 3)* | Real-time webhook fires AND periodic scan catches the same email |
| User forwards same email twice | User hits forward twice on mobile |
| Multiple inbox connections | User has both Gmail and Outlook connected, same receipt in both |
| Multi-account bank alerts | Same purchase generates alerts from both checking and credit card |

---

## Detection Architecture

Duplicate detection runs at two levels:

1. **Email-level deduplication** — same email message, before extraction
2. **Transaction-level deduplication** — same underlying transaction, from different email sources

---

## Level 1: Email-Level Deduplication

**Trigger:** Before creating an `ImportedEmail` record.

**Method:** RFC 5322 `Message-ID` header uniqueness per user.

```python
def check_email_duplicate(user_id: UUID, message_id: str | None) -> bool:
    if message_id is None:
        return False  # can't deduplicate without message_id
    
    existing = db.query(ImportedEmail).filter_by(
        user_id=user_id,
        message_id=message_id
    ).first()
    
    return existing is not None
```

- `message_id` is indexed with a UNIQUE constraint per `(user_id, message_id)`
- If duplicate detected: skip pipeline, increment `scanned_email_count` but not `emails_imported`
- Forwarded emails re-use the original `Message-ID`, so forwarding twice is caught here

---

## Level 2: Transaction-Level Deduplication

**Trigger:** After `ExtractionResult` is created, before `PendingTransaction`.

**Purpose:** Catches the bank-alert + merchant-receipt case, where two *different* emails represent the *same* financial transaction.

### Step 1: Fingerprint Generation

```python
def generate_fingerprint(result: ExtractionResult) -> str:
    normalized_amount = f"{result.amount:.2f}"
    normalized_merchant = normalize_merchant(result.merchant)
    normalized_date = str(result.transaction_date)
    
    raw = f"{normalized_amount}|{normalized_merchant}|{normalized_date}"
    return hashlib.sha256(raw.encode()).hexdigest()
```

**Normalization:**
```python
def normalize_merchant(name: str) -> str:
    if name is None:
        return ""
    name = name.lower().strip()
    name = re.sub(r"[^a-z0-9\s]", "", name)  # strip punctuation
    name = re.sub(r"\s+", " ", name)           # normalize whitespace
    # Strip common suffixes
    for suffix in [" inc", " llc", " ltd", " corp", " co"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    return name.strip()
```

### Step 2: Exact Fingerprint Match

```python
def find_exact_matches(fingerprint: str, user_id: UUID) -> list[CandidateMatch]:
    # Check pending transactions
    pending = db.query(PendingTransaction).filter_by(
        user_id=user_id,
        fingerprint=fingerprint
    ).all()
    
    # Check approved transactions
    approved = db.query(ApprovedTransaction).filter_by(
        user_id=user_id,
        fingerprint=fingerprint
    ).all()
    
    return [
        CandidateMatch(candidate_type="pending", candidate=p, confidence=1.0)
        for p in pending
    ] + [
        CandidateMatch(candidate_type="approved", candidate=a, confidence=1.0)
        for a in approved
    ]
```

### Step 3: Fuzzy Matching

Runs if no exact fingerprint match is found.

```python
def find_fuzzy_matches(
    result: ExtractionResult,
    user_id: UUID,
    date_window_days: int = 3,
    amount_tolerance: float = 0.01,
) -> list[CandidateMatch]:
    
    date_min = result.transaction_date - timedelta(days=date_window_days)
    date_max = result.transaction_date + timedelta(days=date_window_days)
    amount_min = result.amount * (1 - amount_tolerance)
    amount_max = result.amount * (1 + amount_tolerance)
    
    # PostgreSQL query using pg_trgm for merchant similarity
    candidates = db.execute("""
        SELECT id, merchant_normalized, amount, transaction_date,
               'pending' AS candidate_type,
               similarity(merchant_normalized, :merchant) AS merchant_sim
        FROM pending_transactions
        WHERE user_id = :user_id
          AND transaction_date BETWEEN :date_min AND :date_max
          AND amount BETWEEN :amount_min AND :amount_max
          AND similarity(merchant_normalized, :merchant) > 0.5
        
        UNION ALL
        
        SELECT id, merchant, amount, transaction_date,
               'approved' AS candidate_type,
               similarity(merchant, :merchant) AS merchant_sim
        FROM approved_transactions
        WHERE user_id = :user_id
          AND transaction_date BETWEEN :date_min AND :date_max
          AND amount BETWEEN :amount_min AND :amount_max
          AND similarity(merchant, :merchant) > 0.5
        
        ORDER BY merchant_sim DESC
        LIMIT 10
    """, {
        "user_id": user_id,
        "merchant": normalize_merchant(result.merchant),
        "date_min": date_min,
        "date_max": date_max,
        "amount_min": amount_min,
        "amount_max": amount_max,
    })
    
    return [
        CandidateMatch(
            candidate_type=row.candidate_type,
            candidate_id=row.id,
            confidence=compute_candidate_similarity(result, row)
        )
        for row in candidates
    ]
```

### Step 4: AI Disambiguation (Optional, High-Stakes)

When fuzzy confidence is between 0.60–0.80, optionally invoke Claude Haiku to confirm:

```python
async def ai_disambiguate(candidate_a: dict, candidate_b: dict) -> float:
    prompt = f"""Are these two records likely the same financial transaction?

Record A (email 1):
Merchant: {candidate_a['merchant']}
Amount: {candidate_a['amount']} {candidate_a['currency']}
Date: {candidate_a['transaction_date']}
Source: {candidate_a['source']} ({candidate_a['email_type']})

Record B (email 2):
Merchant: {candidate_b['merchant']}
Amount: {candidate_b['amount']} {candidate_b['currency']}
Date: {candidate_b['transaction_date']}
Source: {candidate_b['source']} ({candidate_b['email_type']})

Common case: a bank alert and a merchant receipt for the same purchase.

Respond with JSON: {{"same_transaction": bool, "confidence": float, "reasoning": str}}"""

    response = claude.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    result = json.loads(response.content[0].text)
    return result["confidence"] if result["same_transaction"] else 0.0
```

**When NOT to use AI disambiguation:**
- More than 5 candidates (too expensive)
- All candidates below 0.50 similarity (clearly not duplicates)
- Exact fingerprint match already found (100% certain)

---

## Decision Matrix

```
exact message_id match?
  YES → skip pipeline (email-level dedup)
  NO ↓

exact fingerprint match (amount+merchant+date)?
  YES → duplicate_confidence = 1.0 → auto-suppress
  NO ↓

fuzzy match > 0.95?
  YES → duplicate_confidence = fuzzy_score → auto-suppress
  NO ↓

fuzzy match 0.60–0.95?
  YES → create DuplicateMatch record
      → pending_transaction.status = needs_review
      → show "Possible Duplicate" badge in UI
  NO ↓

fuzzy match < 0.60?
  → no duplicate detected
  → pending_transaction.status = pending_review (normal)
```

---

## Handling Suppressed Duplicates

Auto-suppressed transactions (confidence ≥ 0.95) are not shown in the default review queue but are accessible:

```
GET /pending-transactions?status=duplicate_suppressed

Returns: list of suppressed transactions with their matched duplicates
```

User can override suppression:
```
POST /pending-transactions/{id}/unsuppress
→ status changes to pending_review
→ DuplicateMatch remains visible as context
```

---

## Edge Cases

| Case | Handling |
|------|----------|
| Partial refund | Same merchant + close date but different amount → NOT a duplicate (amount tolerance too small) |
| Split payment | $100 split across two cards: $60 + $40 → neither matches $100 from bank alert → not duplicate |
| Subscription on same date each month | Netflix $15.99 on May 1 and June 1 → date window prevents cross-month collision |
| Same transaction in 3 emails | Email 1 creates pending. Email 2 exact-duped. Email 3 exact-duped. Only 1 pending created. |
| Bank alert arrives 2 days after receipt | date_window_days=3 catches this window |
| Merchant name changes | "Uber Technologies" vs "Uber *TRIP" → trigram similarity < 0.5 → NOT flagged as duplicate. User resolves. |

---

## Performance Considerations

- Fuzzy queries use `pg_trgm` GIN index — require `CREATE EXTENSION pg_trgm`
- Fingerprint index on `(amount, merchant_normalized, transaction_date)` handles exact match in O(log n)
- Fuzzy query is bounded: date window limits search to ~6 days of transactions per user
- AI disambiguation is optional and rate-limited to prevent cost spikes

---

## Future Enhancements

- **Vector embeddings**: Embed merchant + subject + amount together; find similar transactions using pgvector. Handles more fuzzy semantic duplicates.
- **Cross-account detection**: Detect if transfer appeared as both debit in one account and credit in another.
- **Learning from resolutions**: When user resolves "keep_both" on a false positive, record the pair to improve future scoring.

---

*See [ai-processing/confidence-scoring.md](../ai-processing/confidence-scoring.md) for confidence score thresholds.*
