# Database / Entity Schema — G. Full Entity Design

> ⚠️ **PARTIALLY SUPERSEDED (pre-redesign).** Core extraction tables are current, but: there is **no `users` table** (users are external, keyed by `external_user_id`), **`inbox_connections`** is Phase 3, and **`celery_task_id` / `clerk_user_id`** columns are obsolete. The **`webhook_outbox`** table (Phase 7) is not yet shown here. Authoritative model list: [PLAN.md](../../PLAN.md) Phase 3.

## Design Principles
- All primary keys are UUIDs (v4)
- All timestamps are UTC, stored as `timestamptz`
- Sensitive fields (tokens, raw content) are never stored in plain text
- `JSONB` used for variable metadata to avoid migration churn on new email formats
- Soft deletes on User and high-value records; hard deletes on raw content
- Indexes designed for the most common query patterns

---

## Entity Relationship Overview

```
User
 ├── ForwardingAddress (1:1)
 ├── UserPreferences (1:1)
 ├── InboxConnection (1:N)
 ├── ImportedEmail (1:N)
 │    ├── EmailAttachment (1:N)
 │    ├── EmailClassification (1:1)
 │    └── ExtractionResult (1:N) ← multi-tx emails
 │         └── ExtractionSnippet (1:1)
 ├── PendingTransaction (1:N)
 │    ├── DuplicateMatch (1:N)
 │    └── ApprovedTransaction (1:1)
 ├── MerchantRule (1:N)
 ├── CategoryRule (1:N)
 ├── ImportJob (1:N)
 └── AuditLog (1:N)
```

---

## users

```sql
CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    clerk_user_id       VARCHAR(255) UNIQUE NOT NULL,
    email               VARCHAR(320) NOT NULL,
    display_name        VARCHAR(255),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ,                    -- soft delete
    deletion_scheduled  TIMESTAMPTZ                     -- hard delete date
);

CREATE INDEX idx_users_clerk_id ON users(clerk_user_id);
CREATE INDEX idx_users_email ON users(email);
```

**Notes:**
- `deleted_at` set immediately on deletion request
- `deletion_scheduled` = 30 days after deletion request
- Background job hard-deletes all user data when `deletion_scheduled` passes

---

## user_preferences

```sql
CREATE TABLE user_preferences (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    default_currency            CHAR(3) NOT NULL DEFAULT 'USD',
    timezone                    VARCHAR(100) NOT NULL DEFAULT 'UTC',
    review_notification_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    retention_days              INTEGER NOT NULL DEFAULT 90,
    auto_approve_threshold      DECIMAL(3,2),          -- null = no auto-approve
    budget_app_webhook_url      TEXT,
    budget_app_webhook_secret   TEXT,                  -- stored encrypted
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_user_prefs_user_id ON user_preferences(user_id);
```

---

## forwarding_addresses

```sql
CREATE TABLE forwarding_addresses (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    address_hash    VARCHAR(16) NOT NULL UNIQUE,       -- e.g., "abc12345"
    full_address    VARCHAR(320) NOT NULL UNIQUE,      -- abc12345@fintrack.raksimoni.com
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    emails_received INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deactivated_at  TIMESTAMPTZ
);

CREATE INDEX idx_forwarding_address_hash ON forwarding_addresses(address_hash);
CREATE INDEX idx_forwarding_address_user ON forwarding_addresses(user_id);
```

**Notes:**
- When user regenerates address: set `is_active=false` on old, create new record
- Lookup path: `address_hash` → O(1) index lookup

---

## inbox_connections

```sql
CREATE TABLE inbox_connections (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider                VARCHAR(50) NOT NULL,      -- 'google', 'microsoft'
    nylas_grant_id          VARCHAR(255) UNIQUE,
    connected_email         VARCHAR(320) NOT NULL,
    encrypted_access_token  TEXT,                      -- AES-256-GCM encrypted
    token_iv                VARCHAR(255),
    status                  VARCHAR(50) NOT NULL DEFAULT 'active',
                                                       -- active, expired, revoked, error, needs_reauth
    last_scanned_at         TIMESTAMPTZ,
    scanned_email_count     INTEGER NOT NULL DEFAULT 0,
    error_message           TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at              TIMESTAMPTZ
);

CREATE INDEX idx_inbox_conn_user ON inbox_connections(user_id);
CREATE INDEX idx_inbox_conn_grant ON inbox_connections(nylas_grant_id);
CREATE INDEX idx_inbox_conn_status ON inbox_connections(status);
```

---

## imported_emails

```sql
CREATE TABLE imported_emails (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    inbox_connection_id UUID REFERENCES inbox_connections(id) ON DELETE SET NULL,
    source              VARCHAR(50) NOT NULL,          -- 'forwarded', 'inbox_scan'
    message_id          VARCHAR(998),                  -- RFC 5322 Message-ID header
    from_address        VARCHAR(320) NOT NULL,
    from_name           VARCHAR(255),
    to_address          VARCHAR(320),
    subject             TEXT,
    received_at         TIMESTAMPTZ NOT NULL,
    date_header         TIMESTAMPTZ,
    r2_key              TEXT,                          -- null after deletion
    size_bytes          INTEGER,
    has_attachments     BOOLEAN NOT NULL DEFAULT FALSE,
    status              VARCHAR(50) NOT NULL DEFAULT 'received',
                        -- received, stored, classified, extracted,
                        -- non_financial, extraction_failed,
                        -- duplicate_suppressed, pending_review,
                        -- approved, rejected, archived, deleted
    is_financial        BOOLEAN,
    email_type          VARCHAR(50),
    processing_errors   JSONB,                         -- array of error objects
    spam_score          DECIMAL(5,2),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at          TIMESTAMPTZ
);

CREATE INDEX idx_imported_emails_user ON imported_emails(user_id);
CREATE INDEX idx_imported_emails_status ON imported_emails(user_id, status);
CREATE INDEX idx_imported_emails_source ON imported_emails(user_id, source);
CREATE INDEX idx_imported_emails_received ON imported_emails(user_id, received_at DESC);
CREATE UNIQUE INDEX idx_imported_emails_msgid ON imported_emails(user_id, message_id)
    WHERE message_id IS NOT NULL;
-- Full-text search
ALTER TABLE imported_emails ADD COLUMN search_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(subject,'') || ' ' || coalesce(from_address,''))) STORED;
CREATE INDEX idx_imported_emails_fts ON imported_emails USING GIN(search_tsv);
```

**Notes:**
- `message_id` unique per user prevents re-importing the same email
- `r2_key` is nulled when raw content is deleted; record remains for audit purposes
- Partition by `received_at` month in production

---

## email_attachments

```sql
CREATE TABLE email_attachments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id        UUID NOT NULL REFERENCES imported_emails(id) ON DELETE CASCADE,
    filename        VARCHAR(512),
    content_type    VARCHAR(255),
    size_bytes      INTEGER,
    r2_key          TEXT,                              -- null after deletion
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at      TIMESTAMPTZ
);

CREATE INDEX idx_attachments_email ON email_attachments(email_id);
```

---

## email_classifications

```sql
CREATE TABLE email_classifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id        UUID NOT NULL REFERENCES imported_emails(id) ON DELETE CASCADE,
    is_financial    BOOLEAN NOT NULL,
    email_type      VARCHAR(50),
                    -- bank_alert, credit_card_alert, merchant_receipt,
                    -- subscription_invoice, payment_confirmation,
                    -- refund_notification, unknown_financial, non_financial
    confidence      DECIMAL(4,3) NOT NULL,             -- 0.000 to 1.000
    method          VARCHAR(20) NOT NULL,               -- 'rule', 'ai', 'hybrid'
    model_used      VARCHAR(100),                       -- claude-haiku-4-5 etc
    classified_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_response    JSONB                               -- AI raw response for debugging
);

CREATE UNIQUE INDEX idx_classifications_email ON email_classifications(email_id);
```

---

## extraction_results

```sql
CREATE TABLE extraction_results (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email_id                UUID NOT NULL REFERENCES imported_emails(id) ON DELETE CASCADE,
    extraction_index        INTEGER NOT NULL DEFAULT 0, -- for multi-tx emails
    merchant                TEXT,
    merchant_normalized     TEXT,
    amount                  DECIMAL(15,4),
    currency                CHAR(3),
    transaction_date        DATE,
    email_received_at       TIMESTAMPTZ,
    card_suffix             VARCHAR(10),
    account_reference       VARCHAR(100),
    payment_method          VARCHAR(100),
    sender_address          VARCHAR(320),
    subject                 TEXT,
    category_suggestion     VARCHAR(100),
    transaction_type        VARCHAR(50),               -- debit, credit, refund, transfer
    extraction_confidence   DECIMAL(4,3) NOT NULL,
    duplicate_confidence    DECIMAL(4,3) NOT NULL DEFAULT 0,
    method                  VARCHAR(20) NOT NULL,       -- 'template', 'rule', 'ai', 'hybrid'
    model_used              VARCHAR(100),
    field_confidences       JSONB,                     -- per-field confidence scores
    extracted_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_extraction_email ON extraction_results(email_id);
CREATE INDEX idx_extraction_date ON extraction_results(transaction_date);
CREATE INDEX idx_extraction_merchant ON extraction_results(merchant_normalized);
-- Fingerprint index for duplicate detection
CREATE INDEX idx_extraction_fingerprint ON extraction_results(
    amount, merchant_normalized, transaction_date
);
```

---

## extraction_snippets

```sql
CREATE TABLE extraction_snippets (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    extraction_id       UUID NOT NULL REFERENCES extraction_results(id) ON DELETE CASCADE,
    raw_snippet         TEXT NOT NULL,                 -- excerpt used for extraction
    snippet_type        VARCHAR(50),                   -- 'amount', 'merchant', 'date', 'full'
    start_char          INTEGER,
    end_char            INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_snippets_extraction ON extraction_snippets(extraction_id);
```

---

## pending_transactions

```sql
CREATE TABLE pending_transactions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email_id                UUID NOT NULL REFERENCES imported_emails(id),
    extraction_result_id    UUID NOT NULL REFERENCES extraction_results(id),
    -- Denormalized fields for fast review queue rendering
    merchant                TEXT,
    amount                  DECIMAL(15,4),
    currency                CHAR(3),
    transaction_date        DATE,
    card_suffix             VARCHAR(10),
    payment_method          VARCHAR(100),
    category_suggestion     VARCHAR(100),
    transaction_type        VARCHAR(50),
    extraction_confidence   DECIMAL(4,3),
    duplicate_confidence    DECIMAL(4,3),
    -- User overrides (applied at approval time)
    user_overrides          JSONB,                     -- {merchant, amount, category, notes}
    status                  VARCHAR(50) NOT NULL DEFAULT 'pending_review',
                            -- pending_review, needs_review, approved,
                            -- rejected, duplicate_suppressed, archived
    rejection_reason        VARCHAR(100),
    reviewed_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_pending_tx_user ON pending_transactions(user_id);
CREATE INDEX idx_pending_tx_status ON pending_transactions(user_id, status);
CREATE INDEX idx_pending_tx_date ON pending_transactions(user_id, transaction_date DESC);
CREATE INDEX idx_pending_tx_merchant ON pending_transactions(user_id, merchant);
CREATE UNIQUE INDEX idx_pending_tx_extraction ON pending_transactions(extraction_result_id);
```

---

## approved_transactions

```sql
CREATE TABLE approved_transactions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pending_transaction_id  UUID NOT NULL REFERENCES pending_transactions(id),
    email_id                UUID REFERENCES imported_emails(id),
    -- Final values (extraction + user overrides merged)
    merchant                TEXT,
    amount                  DECIMAL(15,4) NOT NULL,
    currency                CHAR(3) NOT NULL,
    transaction_date        DATE NOT NULL,
    card_suffix             VARCHAR(10),
    account_reference       VARCHAR(100),
    payment_method          VARCHAR(100),
    category                VARCHAR(100),
    transaction_type        VARCHAR(50),
    notes                   TEXT,
    sender_address          VARCHAR(320),
    subject                 TEXT,
    extraction_confidence   DECIMAL(4,3),
    -- Budget app delivery
    webhook_delivered       BOOLEAN NOT NULL DEFAULT FALSE,
    webhook_delivered_at    TIMESTAMPTZ,
    webhook_attempts        INTEGER NOT NULL DEFAULT 0,
    approved_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_approved_tx_user ON approved_transactions(user_id);
CREATE INDEX idx_approved_tx_date ON approved_transactions(user_id, transaction_date DESC);
CREATE INDEX idx_approved_tx_merchant ON approved_transactions(user_id, merchant);
CREATE UNIQUE INDEX idx_approved_tx_pending ON approved_transactions(pending_transaction_id);
```

---

## duplicate_matches

```sql
CREATE TABLE duplicate_matches (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pending_transaction_id  UUID NOT NULL REFERENCES pending_transactions(id) ON DELETE CASCADE,
    candidate_type          VARCHAR(50) NOT NULL,      -- 'pending_transaction', 'approved_transaction'
    candidate_id            UUID NOT NULL,
    duplicate_confidence    DECIMAL(4,3) NOT NULL,
    match_reason            VARCHAR(100),
                            -- 'exact_message_id', 'exact_fingerprint',
                            -- 'fuzzy_amount_merchant_date', 'ai_confirmed'
    resolution              VARCHAR(50),               -- keep_this, keep_other, keep_both, unresolved
    resolved_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_dupes_pending ON duplicate_matches(pending_transaction_id);
CREATE INDEX idx_dupes_candidate ON duplicate_matches(candidate_type, candidate_id);
```

---

## merchant_rules

```sql
CREATE TABLE merchant_rules (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID REFERENCES users(id) ON DELETE CASCADE,
                        -- null = system rule
    match_type          VARCHAR(20) NOT NULL,          -- exact, starts_with, contains, regex
    pattern             TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    category            VARCHAR(100),
    priority            INTEGER NOT NULL DEFAULT 100,   -- lower = higher priority
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    is_system           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_merchant_rules_user ON merchant_rules(user_id, is_active);
CREATE INDEX idx_merchant_rules_system ON merchant_rules(is_system, is_active);
```

---

## category_rules

```sql
CREATE TABLE category_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,
    match_field     VARCHAR(50) NOT NULL,              -- merchant, sender, subject, email_type
    match_type      VARCHAR(20) NOT NULL,              -- exact, contains, starts_with, regex
    pattern         TEXT NOT NULL,
    category        VARCHAR(100) NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 100,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    is_system       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_category_rules_user ON category_rules(user_id, is_active);
```

---

## import_jobs

```sql
CREATE TABLE import_jobs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    inbox_connection_id     UUID REFERENCES inbox_connections(id) ON DELETE SET NULL,
    job_type                VARCHAR(50) NOT NULL,      -- inbox_scan, initial_scan, manual_scan
    celery_task_id          VARCHAR(255),
    status                  VARCHAR(50) NOT NULL DEFAULT 'queued',
                            -- queued, running, completed, failed, cancelled
    lookback_days           INTEGER,
    emails_scanned          INTEGER NOT NULL DEFAULT 0,
    emails_imported         INTEGER NOT NULL DEFAULT 0,
    emails_financial        INTEGER NOT NULL DEFAULT 0,
    errors                  INTEGER NOT NULL DEFAULT 0,
    error_details           JSONB,
    started_at              TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,
    cancelled_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_import_jobs_user ON import_jobs(user_id);
CREATE INDEX idx_import_jobs_status ON import_jobs(status);
CREATE INDEX idx_import_jobs_connection ON import_jobs(inbox_connection_id);
```

---

## audit_logs

```sql
CREATE TABLE audit_logs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID,                              -- null after anonymization
    action          VARCHAR(100) NOT NULL,
                    -- inbox_connected, inbox_disconnected, email_imported,
                    -- email_deleted, transaction_approved, transaction_rejected,
                    -- account_deletion_requested, data_exported, etc.
    resource_type   VARCHAR(100),
    resource_id     UUID,
    metadata        JSONB,                             -- additional context
    ip_address      INET,
    user_agent      TEXT,
    outcome         VARCHAR(20) NOT NULL DEFAULT 'success', -- success, failure
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_user ON audit_logs(user_id, created_at DESC);
CREATE INDEX idx_audit_action ON audit_logs(action, created_at DESC);
-- Partition by month for performance
-- PARTITION BY RANGE (created_at)
```

---

## financial_sender_registry

```sql
CREATE TABLE financial_sender_registry (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain          VARCHAR(253) NOT NULL UNIQUE,      -- e.g., amazon.com
    sender_type     VARCHAR(100),                      -- bank, merchant, payment_processor
    email_types     TEXT[],                            -- expected email_type values
    extraction_template_id UUID,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_sender_registry_domain ON financial_sender_registry(domain);
```

---

## extraction_templates

```sql
CREATE TABLE extraction_templates (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sender_domain   VARCHAR(253) NOT NULL,
    template_name   VARCHAR(255),
    amount_pattern  TEXT,                              -- regex
    merchant_pattern TEXT,
    date_pattern    TEXT,
    card_pattern    TEXT,
    currency_pattern TEXT,
    extra_patterns  JSONB,                             -- additional field patterns
    version         INTEGER NOT NULL DEFAULT 1,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    success_count   INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_templates_domain ON extraction_templates(sender_domain, is_active);
```

---

## Summary: Key Relationships

| Entity | Key Relationships |
|--------|-------------------|
| `users` | Root entity; owns everything |
| `forwarding_addresses` | 1:1 with user (can regenerate) |
| `inbox_connections` | N per user; Nylas grant per connection |
| `imported_emails` | N per user; 1 classification, N extraction_results |
| `extraction_results` | N per email (multi-tx); 1 snippet, 1 pending_tx |
| `pending_transactions` | 1 per extraction; moves to approved or rejected |
| `approved_transactions` | 1 per pending; final canonical transaction |
| `duplicate_matches` | N per pending; references any candidate by type+id |
| `audit_logs` | Append-only; references user and resource |

---

*See [architecture/system-modules.md](../architecture/system-modules.md) for entity usage per module.*
*See [security/privacy-compliance.md](../security/privacy-compliance.md) for deletion cascade details.*
