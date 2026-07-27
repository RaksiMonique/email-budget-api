# Testing Strategy

> ⚠️ **PARTIALLY SUPERSEDED (pre-redesign).** The test-pyramid *approach* holds, but the specific tests here target removed components (Postmark webhooks, Nylas OAuth mocks, Redis). Current testing: `pytest` + `httpx` against the `.eml` fixture corpus and `POST /internal/email-received` — no Postmark/Nylas/Redis. See [PLAN.md](../../PLAN.md) Phase 1 & Phase 10.

## Philosophy

The test pyramid for this system has a strong emphasis on the extraction pipeline — it's the most complex and most likely to regress as AI prompts and templates evolve.

```
         ┌──────────────────┐
         │   E2E Tests      │  (5%)  — Playwright, happy-path flows
         ├──────────────────┤
         │ Integration Tests │  (35%) — Real DB, real queues, test email fixtures
         ├──────────────────┤
         │   Unit Tests      │  (60%) — Pure functions, extractors, validators
         └──────────────────┘
```

---

## Unit Tests (pytest)

**Focus:** Pure functions with no I/O.

```
tests/unit/
  extraction/
    test_amount_parser.py         — test all amount formats
    test_date_parser.py           — test all date formats
    test_merchant_normalizer.py   — test normalization rules
    test_confidence_scorer.py     — test scoring formula
    test_template_extractor.py    — test per-sender templates
    test_general_extractor.py     — test general regex patterns
  classification/
    test_financial_sender_rules.py — test sender domain matching
    test_subject_patterns.py      — test subject regex patterns
  duplicate_detection/
    test_fingerprint.py           — test fingerprint generation
    test_fuzzy_matching.py        — test similarity scoring
    test_duplicate_decision.py    — test threshold decisions
  rules_engine/
    test_merchant_rules.py        — test rule priority cascade
    test_category_rules.py        — test category assignment
```

**Key test: Extraction Matrix**
A fixture with 50+ real email samples (anonymized) across all supported email types, each with known ground truth values. Run on every commit.

```python
@pytest.mark.parametrize("email_fixture,expected", EXTRACTION_GROUND_TRUTH)
def test_extraction_accuracy(email_fixture, expected):
    result = extract_with_templates_and_rules(load_fixture(email_fixture))
    assert_within_tolerance(result.amount, expected.amount, tolerance=0.01)
    assert_merchant_matches(result.merchant, expected.merchant)
    assert result.transaction_date == expected.transaction_date
```

---

## Integration Tests (pytest + httpx + real PostgreSQL)

**Uses:** Real PostgreSQL (Docker), real Redis, no mocks for database layer.

**What gets mocked:**
- Claude API → returns fixture responses
- Nylas API → returns fixture email data
- Postmark webhook → real request to test server
- R2/S3 → moto or real R2 test bucket

**Key integration tests:**

```python
# Full forwarded email pipeline
async def test_forwarded_email_full_pipeline(test_client, db, fixtures):
    # Simulate Postmark webhook delivery
    response = await test_client.post(
        "/webhooks/inbound-email",
        json=fixtures.postmark_amazon_receipt,
        headers={"X-Postmark-Inbound-Token": settings.POSTMARK_INBOUND_TOKEN}
    )
    assert response.status_code == 200
    
    # Wait for pipeline to complete
    await wait_for_pipeline_complete(db, max_wait=10)
    
    # Assert pending transaction was created
    pending = await db.query(PendingTransaction).filter_by(user_id=fixtures.user.id).first()
    assert pending is not None
    assert pending.merchant == "Amazon"
    assert pending.amount == Decimal("45.99")
    assert pending.status == "pending_review"

# Duplicate detection
async def test_duplicate_suppression(test_client, db, fixtures):
    # Create an approved transaction
    approved = await create_approved_transaction(db, fixtures.amazon_tx)
    
    # Send a second email with same amount/merchant/date
    await send_test_email(test_client, fixtures.amazon_receipt_email)
    await wait_for_pipeline_complete(db)
    
    # Assert pending transaction was suppressed as duplicate
    pending = await db.query(PendingTransaction).filter_by(
        extraction_result_id=...
    ).first()
    assert pending.status == "duplicate_suppressed"

# OAuth callback and inbox connection
async def test_oauth_callback(test_client, nylas_mock, db, fixtures):
    state = await generate_oauth_state(test_client, fixtures.user)
    response = await test_client.get(
        f"/api/v1/inbox-connections/callback?code=test_code&state={state}"
    )
    assert response.status_code == 302
    connection = await db.query(InboxConnection).filter_by(
        user_id=fixtures.user.id
    ).first()
    assert connection.status == "active"
```

---

## Email Fixture Library

Anonymized/synthetic email samples for each supported email type:

```
tests/fixtures/emails/
  amazon_receipt.json               — merchant receipt, text body
  amazon_receipt_html.json          — merchant receipt, HTML body only
  chase_bank_alert.json             — bank debit alert
  chase_bank_alert_large.json       — unusual amount
  paypal_payment_sent.json          — payment confirmation
  stripe_invoice.json               — subscription invoice
  apple_receipt.json                — App Store receipt
  netflix_invoice.json              — recurring subscription
  uber_receipt.json                 — ride receipt
  refund_amazon.json                — refund notification
  multi_item_amazon.json            — receipt with multiple items
  bank_statement.json               — multiple transactions in one email
  non_financial_promo.json          — marketing email (should be skipped)
  non_financial_newsletter.json     — newsletter (should be skipped)
  malformed_no_amount.json          — no parseable amount
  foreign_currency_euro.json        — EUR amount
  forwarded_with_header.json        — email with "Fwd:" prefix
  duplicate_pair_bank_merchant.json — pair: bank alert + merchant receipt (same tx)
```

---

## AI Extraction Tests

**Strategy:** Mock the Claude API response but test the full prompt construction and response parsing.

```python
async def test_claude_extraction_parsing():
    """Verify we correctly parse Claude's tool_use response."""
    mock_response = {
        "type": "tool_use",
        "name": "extract_transaction",
        "input": {
            "merchant": "Amazon",
            "amount": 45.99,
            "currency": "USD",
            "transaction_date": "2026-05-06",
            "transaction_type": "debit",
            "extraction_confidence": 0.95
        }
    }
    result = parse_claude_extraction_response(mock_response)
    assert result.merchant == "Amazon"
    assert result.amount == Decimal("45.99")

async def test_claude_extraction_invalid_response():
    """Claude returns malformed JSON — should raise ExtractionError, not crash."""
    mock_response = {"type": "text", "text": "I can't extract that."}
    with pytest.raises(ExtractionError):
        parse_claude_extraction_response(mock_response)
```

**Accuracy regression tests:** Monthly, run actual Claude API calls on the fixture library and compare against ground truth. Alert if accuracy drops by > 5%.

---

## Webhook Tests

```python
async def test_postmark_invalid_token_rejected(test_client):
    response = await test_client.post(
        "/webhooks/inbound-email",
        json={},
        headers={"X-Postmark-Inbound-Token": "wrong_token"}
    )
    # Returns 200 (avoid Postmark retries) but does NOT process
    assert response.status_code == 200
    # Verify nothing was stored
    ...

async def test_postmark_rate_limit_enforced(test_client, redis):
    """After 100 emails in an hour, further emails are rate-limited."""
    for i in range(100):
        await send_test_inbound_email(test_client, address_hash="abc12345")
    
    response = await send_test_inbound_email(test_client, address_hash="abc12345")
    assert response.status_code == 200  # always 200
    # But verify the email was NOT stored
    count = await db.count(ImportedEmail.query.filter_by(user_id=...))
    assert count == 100  # capped
```

---

## Privacy / Deletion Tests

```python
async def test_account_deletion_cascade(test_client, db, r2_mock, fixtures):
    """Full deletion removes all PG records and R2 objects."""
    user = fixtures.user_with_data
    
    await test_client.post(
        "/api/v1/privacy/delete-account",
        json={"confirm": True},
        headers=fixtures.auth_header
    )
    
    # Fast-forward past grace period
    await run_deletion_job(db, user.id)
    
    # Verify PG records deleted
    assert await db.count(ImportedEmail.query.filter_by(user_id=user.id)) == 0
    assert await db.count(PendingTransaction.query.filter_by(user_id=user.id)) == 0
    assert await db.count(InboxConnection.query.filter_by(user_id=user.id)) == 0
    
    # Verify R2 objects deleted
    r2_mock.assert_all_objects_deleted(prefix=f"emails/{user.id}/")
    
    # Verify audit logs anonymized
    logs = await db.query(AuditLog).filter_by(user_id=user.id).all()
    assert all(log.user_id is None for log in logs)
```

---

## E2E Tests (Playwright)

**Focus:** Happy-path user flows from UI perspective.

```
e2e/
  test_forwarding_setup.spec.ts     — copy forwarding address, verify it shows
  test_forward_email.spec.ts        — forward test email, see pending tx appear
  test_review_approve.spec.ts       — approve a pending transaction
  test_review_edit_approve.spec.ts  — edit fields then approve
  test_review_reject.spec.ts        — reject a transaction
  test_duplicate_resolution.spec.ts — resolve a duplicate match
  test_disconnect_inbox.spec.ts     — disconnect inbox, verify data cleared
  test_privacy_delete.spec.ts       — initiate account deletion
```

E2E tests run against a staging environment with a real Postmark test inbox. They are not run on every commit — only on main branch merges and before release.

---

## CI/CD Pipeline

```yaml
on: [push, pull_request]

jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pytest tests/unit/ -v --cov=app --cov-report=xml
    coverage-threshold: 80%

  integration-tests:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
      redis:
        image: redis:7
    steps:
      - run: pytest tests/integration/ -v

  e2e-tests:
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - run: playwright test
```

---

## Observability for Testing

**Extraction accuracy tracking:**
- Log per-email: extraction method, fields found, confidence score, user_corrected (bool)
- Monthly report: extraction accuracy by sender, by method, by email type
- Alert: accuracy drops > 5% on any sender category

**Pipeline success tracking:**
- Log: emails received, classified, extracted, pending_created, approved
- Funnel dashboard: identify where emails are being dropped

---

*See [infrastructure/hosting-deployment.md](../infrastructure/hosting-deployment.md) for CI/CD infrastructure details.*
