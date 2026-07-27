# System Modules — Detailed Design

> ⚠️ **PARTIALLY SUPERSEDED (pre-redesign).** The User (Clerk), Inbox Connection (Nylas), and Celery-task modules are removed or deferred to Phase 3. Extraction / classification / dedup module logic remains broadly valid. Authoritative layout: [project-structure.md](project-structure.md); scope: [PLAN.md](../../PLAN.md).

## E. System Modules

---

## Module 1: User / Account Module

### Purpose
Manages user identity, preferences, account settings, unique forwarding address provisioning, and user-level data ownership.

### Responsibilities
- Create and manage user accounts (via Clerk webhook events)
- Provision a unique inbound forwarding email address per user at signup
- Store user preferences (default currency, review notification settings, retention policy)
- Expose account management endpoints
- Handle account deletion with full downstream data erasure

### Internal Flows

```
Clerk webhook: user.created
  → create User record in PG
  → generate unique forwarding address (hash-based, e.g., {8-char-hex}@fintrack.raksimoni.com)
  → store ForwardingAddress record
  → create default UserPreferences record

Clerk webhook: user.deleted
  → trigger data deletion pipeline (see PrivacySecurityModule)
  → soft-delete user, schedule hard delete
```

### Important Components
- `User` entity: id, clerk_user_id, email, created_at, deleted_at
- `ForwardingAddress` entity: user_id, address_hash, full_address, is_active, created_at
- `UserPreferences` entity: default_currency, timezone, review_notification_enabled, retention_days
- `UserService`: provisioning logic, preference CRUD
- Clerk webhook handler: listens for lifecycle events

### Important Edge Cases
- User changes primary email in Clerk — sync to User record
- Duplicate Clerk webhook delivery — idempotent handler using Clerk event ID
- Forwarding address collision — use cryptographic random hash with uniqueness check
- Account deletion while active Celery jobs are processing emails — jobs must check user active status before committing

### Future Scalability
- Multi-tenant: support teams/organizations sharing an inbox connection
- Role-based access: admin users who can view team member spending
- SSO/SAML for enterprise users

---

## Module 2: Inbox Connection Module

### Purpose
Manages OAuth-based connections to users' email inboxes (Gmail, Outlook) through the Nylas API, controls scanning behavior, and handles token lifecycle.

### Responsibilities
- Generate OAuth authorization URLs via Nylas
- Handle OAuth callback and store encrypted access grants
- Track connection health (active, expired, revoked, error)
- Schedule and execute periodic inbox scans via Celery Beat
- Apply email filters (financial senders, date ranges) during scans
- Support disconnect with token revocation
- Limit scope to `email.readonly` — never request send permissions

### Internal Flows

```
Connect flow:
  GET /api/v1/inbox-connections/auth-url?provider=google
    → Nylas.generate_auth_url(scopes=["email.readonly"])
    → return redirect URL to client

OAuth callback:
  GET /api/v1/inbox-connections/callback?code=...
    → Nylas.exchange_code(code) → {grant_id, access_token}
    → encrypt access_token, store InboxConnection
    → enqueue: initial_inbox_scan(connection_id, lookback_days=30)

Scheduled scan (Celery Beat every 15 min):
  scan_inbox(connection_id)
    → fetch emails since last_scanned_at from Nylas
    → filter: sender domain in financial_sender_whitelist
              OR subject matches financial_keyword_pattern
    → for each matched email: enqueue email_pipeline(email_id)

Nylas webhook (real-time):
  POST /webhooks/nylas
    → validate Nylas signature
    → lookup connection by grant_id
    → enqueue email_pipeline(nylas_email_id)

Disconnect:
  DELETE /api/v1/inbox-connections/{id}
    → Nylas.revoke_grant(grant_id)
    → mark InboxConnection.status = revoked
    → enqueue: purge_inbox_emails(connection_id) per user retention policy
```

### Important Components
- `InboxConnection` entity: user_id, provider, nylas_grant_id, encrypted_access_token, status, last_scanned_at, scanned_email_count
- `NylasService`: OAuth URL generation, token exchange, email fetching, grant revocation
- `InboxScanJob` (Celery): scheduled scan task
- `InboxFilter`: rules for which emails to import from a scanned inbox

### Important Edge Cases
- OAuth token expires mid-scan — Nylas refreshes automatically, but handle 401 errors by marking connection as `needs_reauth` and notifying user
- User revokes Google access externally (via Google account settings) — Nylas webhook fires `grant.expired`; update connection status
- User has 10,000 unread emails — initial scan must paginate, respect Nylas rate limits, and be cancellable
- Two scan jobs running concurrently for same connection — use Redis distributed lock keyed on connection_id
- Nylas API outage — jobs retry with exponential backoff; connection health degrades gracefully

### Future Scalability
- Support IMAP fallback for providers not covered by Nylas
- User-configurable scan frequency (real-time, hourly, daily)
- Label/folder filtering: only scan "Receipts" label
- Support Yahoo Mail, iCloud Mail via Nylas extensions

---

## Module 3: Forwarded Email Module

### Purpose
Processes emails that users manually forward to their unique `@fintrack.raksimoni.com` address. Validates sender identity, routes to correct user, and feeds into the shared processing pipeline.

### Responsibilities
- Receive Postmark Inbound Parse webhook
- Validate Postmark webhook signature
- Route inbound email to correct user via forwarding address lookup
- Detect and reject spam / abuse patterns
- Parse MIME content from Postmark's pre-parsed payload
- Enqueue email for the shared classification pipeline

### Internal Flows

```
Postmark delivers POST /webhooks/inbound-email:
  {
    "To": "abc123@fintrack.raksimoni.com",
    "From": "receipts@amazon.com",
    "Subject": "Your order receipt",
    "TextBody": "...",
    "HtmlBody": "...",
    "Attachments": [...],
    "Headers": [...],
    "MessageID": "..."
  }

Handler:
  1. Verify Postmark webhook token (header: X-Postmark-Inbound-Token)
  2. Extract forwarding address: abc123
  3. Lookup ForwardingAddress by address_hash → user_id
  4. If not found → return 200 (swallow silently to avoid address enumeration)
  5. Check abuse: rate limit per forwarding_address (max 100/hour)
  6. Check spam score from Postmark SpamScore header
  7. If spam_score > threshold → store as spam, skip pipeline
  8. Store ImportedEmail record (source=forwarded, status=received)
  9. Upload raw email to R2 (encrypted)
  10. Enqueue: process_email(email_id)
  11. Return 200 immediately (Postmark requires fast response)
```

### Important Components
- `InboundWebhookHandler`: FastAPI endpoint, signature validation, routing
- `ForwardingAddressLookup`: O(1) lookup by address hash
- `SpamFilter`: Postmark spam score check + rate limit + known-bad sender list
- `AbuseRateLimiter`: Redis-based per-address rate limiter
- `MIMEExtractor`: Extract text body, HTML body, attachment list from Postmark payload

### Anti-spam and Abuse Protections
- **Postmark signature validation** — reject requests without valid token
- **Rate limiting** — max 100 emails/hour per forwarding address (Redis token bucket)
- **Spam score threshold** — Postmark provides SpamAssassin score; flag above 5.0
- **Bounced address detection** — if forwarding address generates complaints, auto-disable
- **Large attachment limit** — reject emails with attachments >25MB total
- **Known spam domain blocklist** — static list of known spam domains; reject silently
- **DMARC/SPF validation** — Postmark provides pass/fail; log failures, flag for review

### Important Edge Cases
- Forwarded email arrives before user account is fully provisioned — queue with short delay retry
- User's email client adds a `Fwd:` prefix — strip to recover original subject for classification
- Email chain forwarded — only process the most recent email in the chain
- Same email forwarded twice — deduplication in `detect_duplicates` handles this; don't fail here
- Postmark retry storm (Postmark retries if no 2xx response) — return 200 immediately, process async

### Future Scalability
- Support multiple forwarding addresses per user (e.g., one per card/account)
- Custom domain support: receipts@yourdomain.com → forwarded to inbound pipeline
- Inbound email parsing via AWS SES if Postmark becomes a bottleneck
- Auto-reply confirmation to user: "We received your receipt from Amazon"

---

## Module 4: Email Storage Module

### Purpose
Stores raw email content securely in R2 object storage and structured metadata in PostgreSQL. Acts as the authoritative record of what was imported.

### Responsibilities
- Store the raw MIME email (or Postmark JSON payload) in R2
- Extract and store normalized metadata in PostgreSQL
- Store email attachments (PDF receipts, images) in R2
- Manage retention policies and scheduled deletion
- Provide a retrieval API for downstream modules

### Internal Flows

```
store_email(raw_payload, user_id, source):
  1. Generate email_id (UUID)
  2. Encrypt raw payload (AES-256-GCM, user-derived key envelope)
  3. Upload to R2: emails/{user_id}/{email_id}/raw.json
  4. Extract metadata: message_id, from, to, subject, date, content_type
  5. Create ImportedEmail record:
     { id, user_id, source, message_id, from_address, subject,
       received_at, r2_key, status=received, size_bytes }
  6. For each attachment:
     - Upload to R2: emails/{user_id}/{email_id}/attachments/{filename}
     - Create EmailAttachment record

retrieve_email_content(email_id, user_id):
  1. Verify user owns email_id
  2. Fetch r2_key from ImportedEmail
  3. Download + decrypt from R2
  4. Return parsed content

delete_email(email_id):
  1. Delete R2 object (raw + attachments)
  2. Delete EmailAttachment records
  3. Null out r2_key in ImportedEmail, set status=deleted
  4. Audit log entry: email_deleted
```

### Important Components
- `ImportedEmail` entity — see [database/entity-schema.md](../database/entity-schema.md)
- `EmailAttachment` entity
- `R2StorageService`: upload, download, delete operations
- `EmailEncryptionService`: per-user key envelope (KMS-managed master key)
- `RetentionScheduler`: Celery Beat task that purges expired emails

### Important Edge Cases
- R2 upload fails after PostgreSQL record created — compensating transaction: delete PG record, retry upload, or mark email as `storage_failed`
- Email content is pure-image (no text body) — store raw, flag for OCR-based extraction in future
- Attachment is password-protected PDF — store encrypted, flag as `extraction_failed` with reason
- User requests deletion while Celery pipeline is mid-processing — mark status=deletion_pending; pipeline checks this flag before writing extraction results

### Future Scalability
- Cold storage lifecycle: move R2 objects to Glacier equivalent after 90 days
- Per-attachment content extraction (PDF → text) via Apache Tika or AWS Textract
- Email threading: group emails by thread_id for context
- Streaming download for large emails (avoid loading full MIME into memory)

---

## Module 5: Email Classification Module

### Purpose
Determines whether an email is financially relevant and categorizes its type (receipt, bank alert, invoice, etc.) to guide extraction behavior.

### Responsibilities
- Classify emails as financial or non-financial
- Assign an email type from a controlled vocabulary
- Assign a classification confidence score
- Store classification results
- Skip non-financial emails to avoid unnecessary AI processing costs

### Email Types (Controlled Vocabulary)
```
bank_alert          — bank transaction notification
credit_card_alert   — credit card charge notification
debit_card_alert    — debit card charge notification
merchant_receipt    — order/purchase receipt from merchant
subscription_invoice — recurring service invoice
payment_confirmation — payment sent confirmation
refund_notification  — refund or credit notification
transfer_notification — account transfer
atm_withdrawal       — cash withdrawal notice
unknown_financial    — financial-ish but unrecognized pattern
non_financial        — not financially relevant (skip)
```

### Internal Flows

```
classify_email(email_id):
  1. Load email metadata + text content from storage
  2. Stage 1: Rule-based classification
     - Check sender against financial_sender_registry (known bank/merchant domains)
     - Check subject against financial_subject_patterns (regex bank)
     - If matched: assign email_type + confidence=high
  3. Stage 2: AI classification (if Stage 1 confidence < 0.7)
     - Send subject + sender + first 500 chars of body to Claude Haiku
     - Prompt: "Classify this email. Return JSON: {is_financial, email_type, confidence}"
     - Parse and validate response
  4. Create EmailClassification record
  5. If not financial: update ImportedEmail.status = non_financial (stop pipeline)
  6. If financial: update ImportedEmail.status = classified
  7. Enqueue next step: extract_transaction(email_id)
```

### Important Components
- `FinancialSenderRegistry`: database table of known financial sender domains
- `SubjectPatternMatcher`: compiled regex patterns for financial email subjects
- `ClassificationPrompt`: few-shot prompt template for Claude Haiku classification
- `EmailClassification` entity: email_id, is_financial, email_type, confidence, method (rule/ai)

### Important Edge Cases
- Marketing email from a bank ("Get 5% cashback") — classified as non_financial by subject pattern check
- Email from `noreply@amazon.com` with no purchase ("Your wishlist") — sender registry match but body check fails; use AI to confirm
- Forwarded spam disguised as receipt — spam filter in Module 3 catches this first
- Email in non-English language — AI classification handles this; document which languages are supported
- Classification rate limit on Claude API — queue backs up; process in FIFO order, don't drop

### Future Scalability
- Fine-tune a small local classification model on labeled data from production (after 6 months)
- A/B test rule-based vs. AI classification performance
- User-trainable classification: user marks an email as "not financial" → feeds back into classifier

---

## Module 6: Transaction Extraction Module

### Purpose
Extracts structured transaction fields from classified financial emails using a hybrid rules + AI approach.

### Responsibilities
- Run rule-based extraction first (fast, cheap, deterministic)
- Fall back to Claude Sonnet for complex/ambiguous emails
- Validate all extracted fields with Pydantic
- Compute per-field and overall extraction confidence
- Store extraction results and raw snippets
- Handle multi-transaction emails (e.g., bank statements)

### Extracted Fields
| Field | Type | Notes |
|-------|------|-------|
| merchant | string | payee name |
| amount | decimal | absolute value |
| currency | string | ISO 4217 |
| transaction_date | date | date of transaction |
| email_received_at | datetime | from email headers |
| card_suffix | string | last 4 digits if available |
| account_reference | string | account number fragment |
| payment_method | string | e.g., Visa, PayPal, bank |
| sender_address | string | From header |
| subject | string | Subject header |
| category_suggestion | string | from rules engine |
| transaction_type | enum | debit/credit/refund/transfer |
| duplicate_confidence | float | set later by dedup module |
| extraction_confidence | float | 0.0–1.0 |
| raw_snippet | string | excerpt used for extraction |

### Internal Flows

```
extract_transaction(email_id):
  1. Load email content from storage
  2. Check EmailClassification for email_type
  3. Stage 1: Template extraction
     - Look up sender in ExtractionTemplate registry
     - Apply template (regex patterns specific to this sender)
     - If all required fields extracted with high confidence → done
  4. Stage 2: General rule extraction
     - Apply general-purpose regex patterns (amount, date, card suffix)
     - Partial results possible
  5. Stage 3: AI extraction (if confidence < threshold or missing required fields)
     - Prepare context: subject + text body (truncated to 4000 tokens)
     - Send to Claude Sonnet with extraction tool definition
     - Tool response: structured ExtractionResult JSON
  6. Merge results (prefer template > AI where both present)
  7. Validate with Pydantic ExtractionResult model
  8. Compute confidence scores per field
  9. Run MerchantCategoryRulesEngine for category_suggestion
  10. Create ExtractionResult record + ExtractionSnippet record
  11. Update ImportedEmail.status = extracted
  12. Enqueue: detect_duplicates(extraction_id)

Multi-transaction emails:
  - If email_type=bank_statement and AI returns array of transactions
  - Create multiple ExtractionResult records per ImportedEmail
  - Each creates its own PendingTransaction
```

### Important Components
- `ExtractionTemplate`: per-sender regex extraction templates
- `GeneralExtractor`: generic amount/date/currency regex patterns
- `ClaudeExtractionService`: API call, tool definition, response parsing
- `ExtractionResult` entity
- `ExtractionConfidenceScorer`: see [ai-processing/confidence-scoring.md](../ai-processing/confidence-scoring.md)
- `ExtractionResultValidator`: Pydantic model with field-level validation

### Important Edge Cases
- Amount in non-standard format ("1.234,56" European notation) — handle in amount parser
- Multi-currency transaction — store original currency + converted if available
- Email contains only an image of a receipt (no text) — flag as `needs_ocr`, extraction_confidence=0
- Partial extraction (got amount but not merchant) — create extraction with available fields + flag
- AI returns plausible but wrong amount (hallucinates) — confidence scoring penalizes mismatch with found regex amount
- Non-standard date format — use `dateparser` library with locale hints from sender

### Future Scalability
- OCR pipeline for image-only receipts (AWS Textract or Google Vision)
- PDF parsing for invoice attachments
- Per-user extraction preference learning (user corrections feed back to templates)
- Fine-tuned local extraction model to reduce Claude API costs

---

## Module 7: Merchant / Category Rules Engine

### Purpose
Enriches extracted transactions with category suggestions and normalizes merchant names using a layered rules system.

### Responsibilities
- Normalize merchant names (e.g., "AMZN*MARKETPLACE" → "Amazon")
- Suggest a spending category based on merchant, email type, and keywords
- Allow users to define custom merchant-to-category mappings
- Apply rules in priority order: user rules > system rules > AI suggestion

### Category Vocabulary
```
food_and_dining       — restaurants, food delivery
groceries             — supermarkets, grocery stores
transport             — Uber, Lyft, transit, fuel
entertainment         — streaming, concerts, games
shopping              — retail, Amazon, online stores
utilities             — electricity, water, internet
subscriptions         — SaaS, streaming subscriptions
health                — pharmacy, doctor, gym
travel                — flights, hotels, car rental
atm_cash              — ATM withdrawals
transfers             — bank transfers
other                 — uncategorized
```

### Internal Flows

```
enrich_transaction(extraction_result_id):
  1. Load extraction result (merchant, amount, email_type)
  2. Normalize merchant name:
     - Check MerchantAlias table: "AMZN*" → "Amazon"
     - Apply string cleaning (strip transaction IDs, suffixes)
  3. Rule priority cascade:
     a. User-defined rules: merchant_name LIKE 'Netflix*' → category=subscriptions
     b. System rules: email_type=subscription_invoice → category=subscriptions
     c. Sender-based rules: @uber.com → category=transport
     d. Keyword rules: subject contains "fuel" → category=transport
     e. AI fallback: ask Claude Haiku "what category is this purchase?"
  4. Assign category_suggestion + category_confidence
  5. Update ExtractionResult with enriched fields
```

### Important Components
- `MerchantAlias` entity: pattern, normalized_name, category
- `CategoryRule` entity: user_id (null=system), match_type, pattern, category, priority
- `MerchantNormalizer`: applies alias patterns
- `CategoryRuleEngine`: ordered rule evaluation
- `SystemRuleRegistry`: built-in rules seeded at startup

### Important Edge Cases
- New merchant not in any rule set — AI categorizes as best guess, flagged for user confirmation
- Same merchant in multiple categories (e.g., Amazon can be shopping, subscriptions, groceries) — use email_type and subject context to disambiguate
- User rule conflicts with system rule — user rules always win
- Rule performance at scale — compile patterns to regex at startup, not per request

### Future Scalability
- Community rule library: high-confidence user rules promoted to system rules
- Merchant database integration (e.g., Plaid enrichment API for merchant metadata)
- Subcategory support for detailed budgeting
- User can train the system by correcting categories

---

## Module 8: Pending Transaction Review Module

### Purpose
Manages the review queue where users inspect, edit, approve, or reject extracted transaction candidates before they enter the approved ledger.

### Responsibilities
- Maintain the pending transaction queue per user
- Expose review, approve, edit, and reject endpoints
- Apply bulk actions (approve all from sender, approve all above confidence threshold)
- Track review history
- Notify budget app when new pending transactions arrive (webhook/push)

### Transaction States
```
pending_review   → awaiting user action
approved         → user approved, sent to budget app
rejected         → user rejected, excluded from budget
needs_review     → flagged for manual inspection (low confidence, duplicate suspected)
duplicate        → marked as duplicate of another transaction
expired          → not reviewed within retention window, auto-archived
```

### Internal Flows

```
Review queue fetch:
  GET /api/v1/pending-transactions
    → paginated, sorted by received_at DESC
    → include: extraction_result, duplicate_matches, confidence scores

Approve:
  POST /api/v1/pending-transactions/{id}/approve
    → optionally accept edited fields in body
    → create ApprovedTransaction record
    → if budget_app_webhook configured: POST to budget app
    → update PendingTransaction.status = approved

Reject:
  POST /api/v1/pending-transactions/{id}/reject
    → mark PendingTransaction.status = rejected
    → record rejection reason (optional)

Bulk approve:
  POST /api/v1/pending-transactions/bulk-approve
    → body: {filter: {min_confidence: 0.9, sender: "amazon.com"}}
    → approve all matching transactions

Edit + Approve:
  PATCH /api/v1/pending-transactions/{id}
  POST  /api/v1/pending-transactions/{id}/approve
    → store override fields in PendingTransaction.user_overrides JSONB
    → ApprovedTransaction uses merged (extracted + override) values
```

### Important Components
- `PendingTransaction` entity: extraction_result_id, status, user_overrides (JSONB), reviewed_at
- `ApprovedTransaction` entity: merged canonical transaction data
- `ReviewNotificationService`: sends webhook/push to budget app
- `BulkActionProcessor`: processes bulk approve/reject

### Important Edge Cases
- User approves then wants to undo — allow undo within 60 seconds (soft delete + restore)
- Budget app webhook delivery fails — retry with exponential backoff; queue outbox pattern
- Same transaction appears as both pending and a prior approved (duplicate) — show duplicate badge in UI
- User edits amount to different value — store original + override, audit log the change
- Large backlog (1000+ pending) — paginate, support filtering by date/merchant/confidence

### Future Scalability
- Smart suggestions: "You usually approve all Amazon transactions with confidence >0.9, approve now?"
- Auto-approve rules: user can set rules for trusted senders + high confidence
- Budget app push notifications when new high-confidence transactions arrive
- Recurring transaction detection: "This looks like your monthly Netflix charge"

---

## Module 9: Duplicate Detection Module

### Purpose
Identifies likely duplicate transactions before they enter the pending queue to prevent users from double-counting spending.

### Responsibilities
- Generate a transaction fingerprint for each extraction result
- Compare fingerprint against existing transactions (pending + approved)
- Assign a duplicate_confidence score (0.0–1.0)
- Surface probable duplicates to the user for resolution
- Handle the common case: bank alert + merchant receipt for the same purchase

### Why Duplicates Happen
1. User receives both a bank alert AND a merchant receipt for the same purchase
2. User forwards an email that was already imported via inbox connection
3. Scheduled inbox scan overlaps with a Nylas webhook delivery
4. User forwards the same email twice

### Detection Strategy

See [duplicate-detection/duplicate-detection.md](../duplicate-detection/duplicate-detection.md) for full algorithm.

**Summary:**
1. Exact match: same message_id → 100% duplicate
2. Fingerprint match: hash(normalized_amount + normalized_merchant + date) → high confidence
3. Fuzzy match: similar amount + similar merchant name + same date window → medium confidence
4. AI disambiguation: send both candidates to Claude Haiku — "are these the same transaction?"

### Flows

```
detect_duplicates(extraction_id):
  1. Load ExtractionResult fields
  2. Generate fingerprint: SHA-256(amount + merchant_normalized + transaction_date)
  3. Query: existing PendingTransactions + ApprovedTransactions with same fingerprint
     → if exact match: mark as duplicate, confidence=1.0
  4. Fuzzy query: same day ± 1, amount ± 0.01, merchant trigram similarity > 0.8
     → rank candidates by composite score
  5. For top candidates: compute duplicate_confidence
  6. If confidence > 0.95: auto-mark as duplicate (suppress pending transaction)
  7. If confidence 0.6–0.95: create DuplicateMatch record, flag pending as needs_review
  8. If confidence < 0.6: no duplicate, proceed to pending queue
```

### Important Edge Cases
- Partial refund creates a transaction with same merchant but different amount — not a duplicate
- Split payment across two cards — same merchant, same date, but different amounts; not duplicate
- Same subscription invoice forwarded 3 times — detect all three as duplicates of first
- Bank alert arrives 2 days before merchant receipt — date window must be flexible

---

## Module 10: Privacy / Security / Compliance Module

### Purpose
Ensures user data is handled securely, provides audit trails, and enables users to exercise their privacy rights (data access, deletion, export).

### Responsibilities
- Enforce encryption at rest and in transit
- Implement role-based access control
- Audit all sensitive operations
- Provide data export (GDPR article 20)
- Provide complete data deletion (GDPR article 17, CCPA)
- Manage retention policies
- Handle security incidents (breach notification)

See [security/privacy-compliance.md](../security/privacy-compliance.md) for full documentation.

### Key Flows
```
Data deletion request:
  POST /api/v1/privacy/delete-account
  → queue DeleteUserDataJob
  → delete: all ImportedEmails, R2 objects, InboxConnections, Transactions
  → revoke: Nylas grants
  → anonymize: audit logs (retain anonymized for fraud detection)
  → confirm to user via email

Data export request:
  POST /api/v1/privacy/export
  → queue ExportUserDataJob
  → collect: ImportedEmail metadata, approved transactions, rules
  → package as JSON + CSV
  → upload to R2, generate signed URL, email user

Audit logging (every sensitive action):
  → user_id, action, resource_type, resource_id, timestamp, ip_address, outcome
```

---

*See individual module docs in [ingestion/](../ingestion/), [ai-processing/](../ai-processing/), [duplicate-detection/](../duplicate-detection/), [security/](../security/)*
