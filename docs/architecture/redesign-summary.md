# Architectural Redesign — Questions and Decisions

> Source: `docs/architecture/redesign.md` requirements captured 2026-05-17.
> This document supersedes earlier assumptions where they conflict.

---

## Question 1: Is the Original Stack Still the Right Choice?

**Short answer: Partially. FastAPI + PostgreSQL + R2 stay. Nylas, Postmark, Clerk, Celery/Redis, Claude, and Next.js are all removed or deferred.**

### What changes and why

#### Removed: Nylas
The inbox OAuth connection feature is out of scope. This was the primary reason Nylas was in the stack. Without it, Nylas provides nothing. Removed entirely.

#### Removed: Postmark Inbound Parse
Postmark was chosen as the email ingestion gateway. The new architecture uses **Cloudflare Email Routing** instead, which is integrated with Cloudflare R2 and Cloudflare Queues — the same Cloudflare account already used for storage. This avoids a third-party vendor for what Cloudflare does natively and keeps the infrastructure in fewer accounts.

#### Removed: Clerk
Clerk was solving two problems: user authentication and managing the OAuth flow for inbox connections. Since (a) inbox connections are gone and (b) this is now a **service API consumed by the budgeting app** rather than a user-facing product, there are no users directly authenticating against this API. The budgeting app handles user auth. This API uses API key authentication for service-to-service calls. Clerk is unnecessary overhead.

#### Removed: Celery + Redis
The queue is now Cloudflare Queues, which is already in the infrastructure. A Cloudflare Consumer Worker delivers email-ready events to the FastAPI service via HTTP webhook. FastAPI handles async processing with asyncio background tasks (`asyncio.create_task`). For MVP, this is sufficient and eliminates the Redis deployment and Celery complexity entirely. Redis can return in Phase 3 if processing workloads require it.

#### Removed: Claude API (from MVP)
The user explicitly prefers heuristics over AI for a critical reason: **wrong financial data is worse than no data**. A hallucinated merchant name or incorrect amount erodes user trust immediately. The correct approach is:

1. High-coverage regex templates for the top 100+ known financial senders → 95%+ accuracy on covered emails
2. General-purpose regex patterns → handles most standard formats
3. If extraction fails or confidence is below threshold → mark email as **needs manual review** (not fail silently)
4. Never guess — surface uncertainty to the user

AI is deferred to Phase 2 and only introduced for the long tail of unknown sender formats, with strict confidence gating. See [ai-processing/extraction-strategy.md](../ai-processing/extraction-strategy.md).

#### Removed: Next.js Frontend
This application is now a pure backend API. The budgeting app owns all user-facing UI including the transaction review flow. No frontend is built here. The API design changes slightly — it serves the budgeting app as a client, not end users directly.

### Revised Stack

| Layer | Choice | Change from v1 |
|-------|--------|---------------|
| Backend | **FastAPI** (Python) | Unchanged |
| Database | **PostgreSQL 16** | Unchanged |
| ORM | **SQLAlchemy 2.0 async** + Alembic | Unchanged |
| Storage | **Cloudflare R2** | Unchanged |
| Email routing | **Cloudflare Email Routing** | Replaces Postmark |
| Email worker | **Cloudflare Email Worker** (JS) | New |
| Queue | **Cloudflare Queues** | Replaces Celery + Redis |
| Async processing | **asyncio** background tasks | Replaces Celery |
| Authentication | **API key** (service-to-service) | Replaces Clerk |
| Extraction | **Regex templates + heuristics** | Replaces Claude API |
| Inbox connection | **Not in scope** | Removes Nylas |
| Frontend | **Not in scope** | Removes Next.js |
| Hosting | **Railway** (MVP) | Unchanged |
| Monitoring | **Sentry** | Unchanged |
| Testing | **pytest** + **httpx** | Removed Playwright |

---

## Question 2: Separation of Concerns Between This API and the Budgeting App

### Design Principle

This API is a **transaction extraction service**. It knows about emails and how to parse them. It does not know about budgets, spending categories, or user financial goals. The budgeting app knows about all of those things and uses this API as a data source.

Neither service should duplicate the other's domain logic.

---

### This API Owns

| Concern | Why here |
|---------|---------|
| Cloudflare alias management | Email routing is infrastructure this service controls |
| Raw email storage (R2) | Email content is handled and eventually purged by this service |
| MIME parsing | Email format knowledge lives here |
| Financial classification (is this email financial?) | Domain knowledge about financial email patterns |
| Field extraction (merchant, amount, date, currency, card suffix) | Core purpose of this service |
| Merchant normalization ("AMZN MKTP US*" → "Amazon") | Extraction enrichment, internal detail |
| Category **suggestion** (not final assignment) | This service proposes; budgeting app disposes |
| Duplicate detection | Prevents the same transaction appearing twice in extraction results |
| Extraction result state (pending, confirmed, failed) | Lifecycle tracking for extracted data |
| Category feedback ingestion | Receives confirmed categories from budgeting app to improve future suggestions |
| Heuristic learning (merchant → category mapping) | Internal rules updated from feedback |
| Raw email privacy/deletion | This service holds the sensitive email content |
| Audit log of email processing | Processing audit trail for this service |

---

### Budgeting App Owns

| Concern | Why there |
|---------|---------|
| User authentication and sessions | Users log into the budgeting app, not this API |
| User accounts and profiles | Core budgeting app identity |
| **User-defined categories** (the taxonomy) | Budgeting app feature; categories are user-specific |
| **Approved/confirmed transactions** (the ledger) | Final source of truth for spending |
| Category assignment (what category a transaction actually is) | User decision, confirmed in budgeting app |
| Budget rules, periods, and goals | Not relevant to email extraction |
| Spending analysis and reports | Computed from the transaction ledger |
| Notifications to users about new transactions | Budgeting app surfaces the data |
| External account integrations (bank feeds, Plaid) | Separate data sources; budgeting app aggregates |

---

### Shared Interface (API Contract)

```
Budgeting App → Email API:
  POST   /aliases                          Create alias for a user
  DELETE /aliases/{alias}                  Deactivate alias
  GET    /extractions?user_id=&status=     Fetch pending extraction results
  GET    /extractions/{id}                 Get extraction detail
  POST   /extractions/{id}/confirm         Mark as confirmed (user approved in budgeting app)
  POST   /extractions/{id}/dismiss         Mark as dismissed/rejected
  POST   /feedback/category               Send confirmed category for learning
  DELETE /users/{external_id}/data        Privacy deletion trigger from budgeting app

Email API → Budgeting App:
  POST   {webhook_url}  (new extraction result ready for review)
  POST   {webhook_url}  (extraction failed, needs manual entry)
```

**The budgeting app stores its own `Transaction` record.** It does not rely on this API for the approved transaction ledger. It stores a reference (`email_extraction_id`) to link back to this API's record for traceability and raw email access.

---

### Database: What Lives Where

**This API's PostgreSQL database:**
```
aliases                  — Cloudflare alias → user_id mapping
imported_emails          — email metadata, status, r2_key
email_classifications    — is_financial, email_type, confidence
extraction_results       — all extracted fields, confidence scores
extraction_snippets      — raw text excerpts used for extraction
extraction_templates     — per-sender regex templates
merchant_rules           — normalization rules (system + feedback-learned)
category_suggestions     — suggestion log (for feedback correlation)
duplicate_matches        — duplicate detection results
import_audit_logs        — processing audit trail
```

**Budgeting App's database:**
```
users                    — user accounts, auth
categories               — user-defined categories
transactions             — approved transaction ledger
  └─ email_extraction_id — FK reference to email API (not a real FK, just a stored ID)
transaction_feedback     — history of category corrections
budgets                  — budget periods and rules
```

**Rule of thumb:** If a table's rows would need to be deleted when a user revokes email access, it belongs here. If a table's rows should survive even after email integration is disabled, it belongs in the budgeting app.

---

## Question 3: Additional Features for Robustness

### High-Value Additions

**1. Extraction failure workflow**
When extraction fails or confidence is below threshold, don't silently drop the email. Send a webhook to the budgeting app: `{ event: "extraction.failed", email_id, subject, from, received_at }`. The budgeting app can surface this to the user: "We received a receipt from Amazon but couldn't read it — [enter manually]."

**2. Extraction preview endpoint**
`GET /extractions/{id}/preview` — returns the extracted fields alongside the raw snippet, formatted for the budgeting app's review UI. This lets the budgeting app show "we extracted $45.99 from this text: [...]" alongside the edit form.

**3. Category feedback loop**
`POST /feedback/category` — budgeting app sends: `{ extraction_id, merchant_normalized, category_confirmed }`. Email API updates its merchant-to-category heuristics. Over time, suggestions improve without AI. This is pure lookup-table learning.

**4. Re-extraction endpoint**
`POST /extractions/{id}/reprocess` — triggers re-extraction on a stored email if the template has been improved since initial processing. Useful after adding a new sender template to catch existing unprocessed emails.

**5. Alias management API for budgeting app**
Full CRUD for aliases with metadata: `POST /aliases` (create), `GET /aliases?user_id=`, `DELETE /aliases/{id}` (deactivate). The budgeting app calls these on user onboarding and account deletion.

**6. Extraction confidence reporting**
`GET /stats/extraction?user_id=` — returns aggregate confidence metrics: what % of emails extracted successfully, what % failed, top failure reasons. The budgeting app can show users "95% of your emails parsed automatically."

**7. Sender template contribution**
When extraction fails on a new sender, log the failure with the sender domain. Periodically review top failing domains and add templates. Build a `GET /admin/top-failing-senders` endpoint for operational visibility.

**8. Deduplication across ingestion sessions**
If a user has multiple aliases (one per credit card), the same charge could arrive from both a bank alert alias and a merchant receipt alias. Duplicate detection must work across aliases for the same user. Design the dedup lookup as user-scoped, not alias-scoped.

**9. Idempotent confirmation**
`POST /extractions/{id}/confirm` must be idempotent — calling it twice returns the same result rather than erroring. The budgeting app may retry on network failure.

**10. Soft deletion / data portability**
When budgeting app calls `DELETE /users/{id}/data`, cascade to mark all email content as pending deletion (30-day grace period). Provide a `GET /users/{id}/data-export` for GDPR portability before deletion.

---

## MVP Architecture Flow (Proposed)

```
Budgeting App
  → POST /aliases {external_user_id}
  ← {alias: "abc123@fintrack.raksimoni.com"}
  [Cloudflare Email Routing rule created for abc123]

User forwards email to abc123@fintrack.raksimoni.com
  → Cloudflare Email Routing matches alias
  → Cloudflare Email Worker (JS) runs:
      1. Parse MIME headers (from, subject, date, message-id)
      2. Upload raw .eml to R2: emails/{alias}/{email_id}.eml
      3. Push to Cloudflare Queue:
         { email_id, alias, r2_key, from, subject, message_id, received_at }
  → Cloudflare Queue Consumer Worker:
      POST /internal/email-received → FastAPI (with X-Internal-Secret header)

FastAPI /internal/email-received (returns 200 immediately):
  → Validate internal secret
  → Create ImportedEmail record (status=received)
  → asyncio.create_task(process_email(email_id))

Background: process_email(email_id):
  → Lookup alias → user_id
  → Fetch raw .eml from R2
  → MIME parse (Python email library)
  → Classify: financial? (rule-based)
  → Extract: templates → general regex → mark failed if no result
  → Enrich: merchant normalization, category suggestion
  → Duplicate detect
  → Create ExtractionResult (status = pending_review or failed)
  → POST webhook to budgeting app: new extraction available

Budgeting App:
  → GET /extractions/{id} (full details for review UI)
  → User reviews, confirms category
  → POST /extractions/{id}/confirm {category: "shopping"}
  → Budgeting app stores in own Transaction ledger
  → POST /feedback/category {merchant_normalized, category_confirmed}
```

---

*See [integration/budgeting-app-integration.md](../integration/budgeting-app-integration.md) for the full integration contract.*
*See [architecture/stack-decisions.md](stack-decisions.md) for updated stack reasoning.*
*See [ingestion/cloudflare-email-setup.md](../ingestion/cloudflare-email-setup.md) for Cloudflare setup.*
