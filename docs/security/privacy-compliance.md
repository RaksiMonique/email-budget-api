# Privacy, Security, and Compliance — K.

> ⚠️ **PARTIALLY SUPERSEDED (pre-redesign).** GDPR / retention / deletion *principles* still apply, but specifics naming Clerk, Nylas, Postmark, Celery, and Redis are obsolete — this API has no user accounts, no inbox OAuth, and no Redis. Third-party processors are now **Cloudflare** (plus Anthropic only if Phase 3 AI ships). Authoritative: [PLAN.md](../../PLAN.md) Phase 8.

## Threat Model

This system handles:
- OAuth tokens granting access to users' email inboxes
- Raw financial email content (account numbers, card details, transaction history)
- Extracted transaction amounts and merchant data

**Primary threats:**
1. Unauthorized access to raw email content
2. Token theft enabling inbox access without user knowledge
3. Data breach exposing financial patterns or card details
4. SSRF/injection via malicious email content processed by AI
5. Abuse of inbound forwarding addresses (spam amplification)
6. Insider access to raw email content

---

## K.1 Authentication and Authorization

See [security/auth-strategy.md](auth-strategy.md) for detailed auth flow.

**Summary:**
- All API endpoints require Clerk JWT (RS256)
- Webhook endpoints use provider HMAC signatures instead
- Per-request user_id extracted from JWT — never from request body
- All database queries include `WHERE user_id = :user_id` — no cross-user data leakage possible
- Admin routes (if any) use separate role-gated auth

---

## K.2 Encryption

### In Transit
- All external connections: TLS 1.2+ enforced
- Nylas API: HTTPS only
- Postmark: HTTPS only
- R2/S3: HTTPS only
- Internal PostgreSQL + Redis: TLS if on separate nodes (or private VPC with no external exposure)

### At Rest — Database
Sensitive columns encrypted at application layer before storage (not just disk-level):

| Field | Encryption |
|-------|-----------|
| `inbox_connections.encrypted_access_token` | AES-256-GCM, per-user envelope key |
| `user_preferences.budget_app_webhook_secret` | AES-256-GCM |
| Full database | RDS encryption at rest (AES-256) |

### At Rest — R2 Object Storage
- Server-side encryption: AES-256 (R2 default)
- Optional: client-side encryption of raw email content before upload
  - Key: derived from user's master key in AWS KMS
  - Prevents R2 employees or R2 breach from reading email content
  - Key envelope: `user_key = KMS.decrypt(user.encrypted_key_ciphertext)`

### Key Management
- Master keys: stored in AWS KMS (or HashiCorp Vault)
- Per-user encryption keys: derived from master key + user_id using HKDF
- Key rotation: annual, with re-encryption job
- Access to KMS: only API and Celery worker IAM roles

---

## K.3 Data Minimization

Principle: store the minimum necessary data, for the minimum necessary time.

| What | What is stored | What is NOT stored |
|------|---------------|-------------------|
| Raw email content | In R2, encrypted, with TTL | Not in PostgreSQL |
| Email metadata | subject, from, date in PostgreSQL | Full body not in DB |
| Card details | Last 4 digits only (card_suffix) | Full card numbers never |
| Account numbers | Reference only (fragment) | Full account numbers never |
| AI extraction | Result fields + raw_snippet | Full AI prompt/response (debug only) |
| OAuth tokens | Encrypted, in DB | Not in logs, not in Sentry |

**Raw email content lifecycle:**
1. Stored encrypted in R2 on ingestion
2. Retrieved only for extraction pipeline (temporary, in-memory)
3. Auto-deleted after `user.retention_days` (default 90 days)
4. Deleted immediately on user request

**After raw content is deleted:**
- `imported_emails.r2_key` is set to NULL
- Extracted transaction fields remain (no email content, just numbers/merchant)
- User can still delete extracted transactions separately

---

## K.4 Input Sanitization

**Email content sent to AI:**
- HTML is converted to plain text before sending to Claude API
- External URLs are stripped (prevent SSRF via AI prompt injection)
- Base64 images are removed
- Email content is sandboxed: Claude API cannot make outbound calls
- Maximum content length enforced (4000 tokens)

**Prompt injection mitigation:**
- System prompt and user content are strictly separated (not interpolated)
- Structured output (tool_use) prevents freeform injection affecting downstream code
- AI responses are parsed and validated through Pydantic — raw AI text never executed
- Anomalous AI responses (e.g., refusing to extract, outputting code) are caught and logged

**Webhook payloads:**
- Postmark payload is treated as untrusted input
- All fields validated and length-limited before storage
- HTML email body sanitized with `bleach` before display in UI

---

## K.5 Retention and Deletion Policies

### Default Retention

| Data Type | Default Retention | User-Configurable |
|-----------|------------------|------------------|
| Raw email content (R2) | 90 days | Yes (30–365 days or keep forever) |
| Email metadata (PG) | 2 years | No (regulatory minimum) |
| Extracted transactions | 7 years | No (financial record requirement) |
| Audit logs | 7 years | No |
| OAuth access tokens | Until revoked | No |
| Forwarding address | Until account deleted | No |

### Automated Retention Enforcement

```
Celery Beat: daily at 2 AM UTC
  → purge_expired_emails()
  → for each user: find ImportedEmails where received_at < NOW() - retention_days
  → delete R2 objects
  → null r2_key in PG
  → audit_log: email_content_purged
```

### User-Initiated Deletion

**Delete raw email content only:**
```
POST /privacy/delete-emails?older_than_days=30
→ Delete R2 objects, null r2_key
→ Transaction records remain
```

**Delete specific imported email:**
```
DELETE /emails/{id}
→ Delete R2 object, null r2_key, soft-delete ImportedEmail
→ PendingTransaction remains (user already reviewed or can review with metadata only)
```

**Full account deletion:**
```
POST /privacy/delete-account
→ 30-day grace period (user can cancel)
→ After grace period:
  1. Revoke all Nylas grants
  2. Delete all R2 objects
  3. Hard-delete all PG records (cascading)
  4. Anonymize audit_logs (set user_id = NULL)
  5. Delete Clerk account
  6. Send deletion confirmation email
→ audit_log: account_deleted
```

---

## K.6 GDPR / CCPA Compliance

### GDPR Rights

| Right | Implementation |
|-------|---------------|
| Article 15 — Access | GET /privacy/my-data-summary returns summary; POST /privacy/export returns full export |
| Article 17 — Erasure | POST /privacy/delete-account + DELETE /emails/{id} |
| Article 20 — Portability | POST /privacy/export returns JSON + CSV |
| Article 21 — Object to processing | User can disconnect inbox and delete all emails |
| Article 25 — Privacy by design | Data minimization, encryption by default, retention policies |

### Lawful Basis
- Processing basis: **Contract** (user signed up and actively authorized inbox access)
- For each email import: user explicitly authorized via OAuth consent or forwarding
- AI processing of email content: covered under contract basis (stated in Terms of Service)

### Data Processing Record
Document maintained per Article 30 GDPR:
- What data: email metadata + transaction fields extracted from financial emails
- Why: providing spending tracking service
- Retention: 90 days raw content, 7 years transaction records
- Third-party processors: Nylas, Postmark, Anthropic, Cloudflare

---

## K.7 Audit Logging

Every sensitive action creates an audit log entry:

```python
AUDITED_ACTIONS = {
    "inbox_connected",
    "inbox_disconnected",
    "inbox_scan_started",
    "inbox_scan_completed",
    "email_imported",
    "email_content_deleted",
    "email_content_purged",         # automated retention
    "transaction_approved",
    "transaction_rejected",
    "transaction_edited",
    "merchant_rule_created",
    "account_deletion_requested",
    "account_deletion_cancelled",
    "account_deleted",
    "data_export_requested",
    "data_export_completed",
    "raw_email_accessed",           # when user views raw email
    "api_key_created",              # if API key system added later
}
```

Audit logs:
- Are append-only (no updates, no deletes during user lifetime)
- Are anonymized on account deletion (user_id → NULL, IP → NULL)
- Are retained for 7 years post-anonymization for fraud detection
- Are accessible to users via GET /audit-log

---

## K.8 Inbox Access Scope

OAuth consent scope is strictly `email.readonly`. The system:
- Cannot send emails
- Cannot delete emails from user's inbox
- Cannot access drafts or sent items (unless in scope — they won't be)
- Only reads emails matching financial filters
- Reads minimum number of emails necessary (incremental scan since last_scanned_at)
- Never reads emails outside the financial pattern match

This is disclosed in:
1. OAuth consent screen explanation (in app before OAuth redirect)
2. Privacy policy
3. Permission summary in UI (Inbox Connection module)

---

## K.9 Forwarding Address Security

- Addresses are opaque hashes (8-char hex) — not guessable
- No address enumeration: invalid addresses return 200 silently (not 404)
- Rate limited: 100 emails/hour per address
- Spam filtered via Postmark SpamAssassin score
- User can regenerate address at any time (old address immediately deactivated)
- Addresses are never displayed in public logs

---

## K.10 API Security

- Rate limiting: 100 req/min per user (Redis token bucket)
- Webhook endpoint: 500 req/min global (Postmark delivers in batches)
- CORS: only allow configured frontend origins
- Request size limits: 10MB max body (email payload)
- All SQL queries use parameterized statements (SQLAlchemy ORM — no raw string interpolation)
- No sensitive data in URL paths or query params (user IDs in path are fine; tokens are not)
- API errors never leak internal stack traces to client (Sentry captures; client gets generic error)

---

## K.11 Security Incident Response

**If raw email content is breached:**
1. Immediately rotate all R2 access credentials
2. Notify affected users within 72 hours (GDPR requirement)
3. Assess scope: which users' R2 paths were accessible
4. Provide incident report to users

**If OAuth tokens are breached:**
1. Immediately revoke all Nylas grants via Nylas admin API
2. Force re-authentication for all inbox connections
3. Notify users

**If database is breached:**
1. Application-layer encrypted tokens are useless without KMS access
2. Raw email content is in R2, not database — minimal financial data in DB
3. Transaction amounts and merchants are exposed — notify users

---

*See [security/auth-strategy.md](auth-strategy.md) for JWT and OAuth details.*
*See [database/entity-schema.md](../database/entity-schema.md) for encrypted fields.*
