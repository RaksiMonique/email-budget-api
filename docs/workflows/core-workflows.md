# Core Workflows — D. End-to-End Data Flows

> ⚠️ **SUPERSEDED (pre-redesign).** These flows assume Postmark + Celery. The current end-to-end flow (Cloudflare Email Worker → Queue → synchronous FastAPI pipeline → webhook outbox) is in [redesign-summary.md](../architecture/redesign-summary.md) and [PLAN.md](../../PLAN.md).

## Workflow 1: Forwarded Email → Pending Transaction

```
┌─────────────────────────────────────────────────────────────┐
│ TRIGGER: User forwards email to abc123@fintrack.raksimoni.com│
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 1: Inbound Webhook (FastAPI, synchronous)              │
│                                                              │
│ 1a. POST /webhooks/inbound-email (Postmark)                 │
│ 1b. Verify Postmark inbound token                           │
│ 1c. Extract destination: abc123                             │
│ 1d. Lookup user by forwarding_address hash                  │
│ 1e. Check rate limit (Redis token bucket)                   │
│ 1f. Check spam score (Postmark header)                      │
│ 1g. Return HTTP 200 immediately                             │
└──────────────────────────┬──────────────────────────────────┘
                           │ (async, fire-and-forget)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 2: Email Storage (Celery: store_email task)            │
│                                                              │
│ 2a. Parse Postmark JSON payload                             │
│ 2b. Encrypt raw payload                                     │
│ 2c. Upload to R2: emails/{user_id}/{email_id}/raw.json     │
│ 2d. Create ImportedEmail record (status=received)           │
│ 2e. Store attachments in R2 if present                      │
│ 2f. Create EmailAttachment records                          │
│                                                              │
│ On failure: mark ImportedEmail.status=storage_failed        │
│             retry up to 3 times with backoff                │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 3: Email Classification (Celery: classify_email task)  │
│                                                              │
│ 3a. Load email subject, sender, first 500 chars of body     │
│ 3b. Check sender against FinancialSenderRegistry            │
│ 3c. Check subject against financial_subject_patterns        │
│ 3d. If no confident match: call Claude Haiku classification │
│ 3e. Create EmailClassification record                       │
│ 3f. If is_financial=false: stop pipeline, status=non_financial│
│ 3g. If is_financial=true: status=classified                 │
│                                                              │
│ Confidence below 0.5 + is_financial=false: human review flag│
└──────────────────────────┬──────────────────────────────────┘
                           │ (only if financial)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 4: Transaction Extraction (Celery: extract_tx task)    │
│                                                              │
│ 4a. Try ExtractionTemplate for sender (regex-based)         │
│ 4b. If incomplete: try GeneralExtractor (regex patterns)    │
│ 4c. If still incomplete or low confidence: call Claude Sonnet│
│     with tool_use for structured extraction                  │
│ 4d. Merge results, validate with Pydantic                   │
│ 4e. Run MerchantCategoryRulesEngine for category suggestion  │
│ 4f. Compute confidence scores per field + overall           │
│ 4g. Create ExtractionResult record                          │
│ 4h. Create ExtractionSnippet record (raw_snippet)           │
│ 4i. Update ImportedEmail.status = extracted                 │
│                                                              │
│ If extraction fails entirely: status=extraction_failed      │
│                              notify user for manual review  │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 5: Duplicate Detection (Celery: detect_dupes task)     │
│                                                              │
│ 5a. Generate fingerprint: hash(amount+merchant+date)        │
│ 5b. Exact fingerprint match → duplicate_confidence=1.0      │
│ 5c. Fuzzy match: same date±1, amount±0.01, merchant sim>0.8 │
│ 5d. If high confidence duplicate (>0.95): suppress pending  │
│     Create DuplicateMatch, mark status=duplicate_suppressed │
│ 5e. If medium confidence (0.6–0.95): flag needs_review      │
│     Create DuplicateMatch record                            │
│ 5f. If low confidence (<0.6): no duplicate                  │
│                                                              │
│ Outcome: duplicate_confidence assigned to extraction        │
└──────────────────────────┬──────────────────────────────────┘
                           │ (unless suppressed)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│ STEP 6: Create Pending Transaction                          │
│                                                              │
│ 6a. Create PendingTransaction from ExtractionResult fields  │
│ 6b. status=pending_review (or needs_review if flagged)      │
│ 6c. Fire budget app outbound webhook if configured          │
│ 6d. Increment user's pending_count for notification badge   │
│                                                              │
│ Final state: user sees pending transaction in review queue  │
└─────────────────────────────────────────────────────────────┘
```

---

## Workflow 2: Inbox Connection Scan → Pending Transaction

```
TRIGGER: Celery Beat fires scan_inbox(connection_id) every 15 minutes
OR: Nylas fires webhook: new email received

┌─────────────────────────────────────────────────────────────┐
│ STEP 1: Inbox Scan (Celery: scan_inbox task)                │
│                                                              │
│ 1a. Load InboxConnection: check status=active               │
│ 1b. Acquire Redis lock: scan_{connection_id}                │
│ 1c. Call Nylas: list emails since last_scanned_at           │
│     filter: sender OR subject matches financial patterns    │
│ 1d. For each email: check if already imported (message_id)  │
│ 1e. For new emails: fetch full content from Nylas           │
│ 1f. Update last_scanned_at                                  │
│ 1g. Release Redis lock                                      │
│                                                              │
│ Then for each new email → same pipeline as Workflow 1       │
│ starting at STEP 2                                          │
└─────────────────────────────────────────────────────────────┘
```

---

## Workflow 3: User Review → Approved Transaction

```
TRIGGER: User opens Pending Transactions screen

┌─────────────────────────────────────────────────────────────┐
│ GET /api/v1/pending-transactions                            │
│ Returns: paginated list with extraction details             │
│          and duplicate_match info if flagged               │
└──────────────────────────┬──────────────────────────────────┘
                           │
          ┌────────────────┼──────────────────────┐
          │                │                       │
          ▼                ▼                       ▼
   [Approve]          [Edit + Approve]         [Reject]
          │                │                       │
          ▼                ▼                       ▼
   POST /{id}/approve  PATCH /{id}           POST /{id}/reject
                       POST /{id}/approve
          │                │                       │
          └────────────────┼───────────────────────┘
                           │
                           ▼
                  ApprovedTransaction created
                  (or status=rejected)
                           │
                           ▼
              Budget app webhook delivery
              (POST to configured endpoint with tx payload)
```

---

## Workflow 4: OAuth Inbox Connection

```
TRIGGER: User clicks "Connect Gmail"

1. GET /api/v1/inbox-connections/auth-url?provider=google
   ← returns { auth_url: "https://app.nylas.com/..." }

2. User redirected to Google OAuth consent screen
   (scopes: gmail.readonly)

3. Google redirects to /api/v1/inbox-connections/callback?code=...&state=...

4. Validate state parameter (CSRF protection)

5. POST to Nylas: exchange code → { grant_id, access_token }

6. Encrypt access_token (AES-256-GCM)

7. Create InboxConnection record

8. Enqueue: initial_inbox_scan(connection_id, lookback_days=30)

9. Return to frontend: connection created

10. Background scan runs, user sees emails appearing in history
```

---

## Workflow 5: Inbox Disconnect + Data Deletion

```
TRIGGER: User clicks "Disconnect" on inbox connection

1. DELETE /api/v1/inbox-connections/{id}

2. Verify user owns connection_id

3. Call Nylas: revoke_grant(grant_id)
   (Nylas revokes Google/Outlook OAuth token)

4. Mark InboxConnection.status = revoked

5. Based on user's retention preference:
   a. Keep imported emails (default: keep 90 days)
   b. Delete all emails from this connection now (if user requests)

6. Audit log: inbox_disconnected

7. Return 200 to user
```

---

## Workflow 6: Full Account Deletion

```
TRIGGER: POST /api/v1/privacy/delete-account

1. Authenticate user (must re-enter password or confirm)

2. Revoke all InboxConnections (Nylas grants)

3. Cancel all active Celery jobs for this user

4. Delete from R2:
   - All emails/{user_id}/* objects

5. Delete from PostgreSQL (in order):
   - DuplicateMatches
   - ExtractionSnippets
   - ExtractionResults
   - EmailClassifications
   - EmailAttachments
   - ImportedEmails
   - PendingTransactions + ApprovedTransactions
   - InboxConnections
   - ForwardingAddresses
   - UserPreferences
   - ImportJobs
   - User (soft delete → hard delete after 30 days)

6. Audit logs: anonymize (null user_id, keep action+timestamp for fraud detection)

7. Delete Clerk user account

8. Send deletion confirmation email

Processing time: up to 30 days per GDPR requirement
Confirmation: email sent when complete
```

---

## Workflow 7: Error / Extraction Failure Path

```
TRIGGER: extract_transaction fails (AI error, parse error, invalid content)

1. Mark ImportedEmail.status = extraction_failed
2. Store error details in ImportedEmail.processing_errors (JSONB)
3. Create FailedExtraction record with reason
4. Sentry alert if error rate > threshold
5. User notified: "We couldn't automatically read this receipt"
6. User can:
   a. View the email in the app
   b. Manually enter the transaction
   c. Report the parsing failure (feeds into template improvement)
```

---

## Pipeline State Machine

```
ImportedEmail.status transitions:

received
  │
  ├──[spam/rate-limit]──→ spam_rejected (terminal)
  │
  ▼
storage_failed (terminal — retry 3x then fail)
  │
  ▼
received → stored
  │
  ├──[not financial]──→ non_financial (terminal)
  │
  ▼
classified
  │
  ├──[extraction error]──→ extraction_failed (terminal — user review)
  │
  ▼
extracted
  │
  ▼
deduplication_pending
  │
  ├──[duplicate suppressed]──→ duplicate_suppressed (terminal)
  │
  ▼
pending_review (PendingTransaction created)
  │
  ├──[user approves]──→ approved
  ├──[user rejects]──→ rejected
  └──[expires/archived]──→ archived
```

---

*See [architecture/system-modules.md](../architecture/system-modules.md) for module-level details.*
*See [ai-processing/extraction-strategy.md](../ai-processing/extraction-strategy.md) for extraction details.*
