# System Architecture Overview

> Updated 2026-05-17. Reflects Cloudflare-native ingestion, no frontend, and service API model.

## System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        CLOUDFLARE LAYER                             │
│                                                                     │
│  User email → MX records → Cloudflare Email Routing                │
│                                  │                                  │
│                          Email Worker (TS)                          │
│                          ├─ store .eml → R2                        │
│                          └─ push → Cloudflare Queue                │
│                                  │                                  │
│                     Queue Consumer Worker (TS)                      │
│                     └─ POST /internal/email-received               │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────┐
│                        FASTAPI SERVICE                              │
│                                                                     │
│  /internal/email-received  →  BackgroundTasks                       │
│                                      │                              │
│  process_email():                    │                              │
│  ├─ fetch .eml ← R2                  │                              │
│  ├─ MIME parse                       │                              │
│  ├─ classify (rule-based)            │                              │
│  ├─ extract (templates + regex)      │                              │
│  ├─ enrich (merchant norm, category) │                              │
│  ├─ duplicate detect                 │                              │
│  ├─ store → PostgreSQL               │                              │
│  └─ webhook → Budgeting App          │                              │
│                                      │                              │
│  REST API (for Budgeting App):        │                              │
│  /api/v1/aliases                     │                              │
│  /api/v1/extractions                 │                              │
│  /api/v1/feedback                    │                              │
│  /api/v1/config                      │                              │
└──────────────────────────────────────┬──────────────────────────────┘
                                       │
┌──────────────────────────────────────▼──────────────────────────────┐
│                        BUDGETING APP                                │
│                                                                     │
│  Receives webhook: extraction.created / extraction.failed           │
│  Calls GET /extractions/{id} for review data                        │
│  User confirms category → POST /extractions/{id}/confirm            │
│  Sends feedback → POST /feedback/category                           │
│  Stores own Transaction record (with email_extraction_id)           │
└─────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility |
|-----------|----------------|
| **Cloudflare Email Routing** | MX records, catch-all rule → Email Worker |
| **Cloudflare Email Worker** (TS) | Receive MIME email, store .eml to R2, push to Queue |
| **Cloudflare Queue** | Buffer email-processing jobs, retry on failure |
| **Cloudflare Queue Consumer** (TS) | Pull from Queue, POST to FastAPI internal endpoint |
| **Cloudflare R2** | Persistent storage for raw .eml files + attachments |
| **FastAPI** | Internal webhook handler, REST API, async processing |
| **PostgreSQL** | Aliases, email metadata, extraction results, audit logs |
| **Sentry** | Error tracking for FastAPI and Cloudflare Workers |
| **Budgeting App** | Client — owns users, categories, approved transactions, UI |

## Request Flow: Forwarded Email → Extraction

```
1.  User forwards receipt to abc123@fintrack.raksimoni.com
2.  Cloudflare Email Routing matches catch-all rule → triggers Email Worker
3.  Email Worker:
    a. Reads raw MIME stream
    b. Uploads raw .eml to R2: emails/abc123/{email_id}.eml
    c. Pushes to Cloudflare Queue:
       { email_id, alias_hash: "abc123", r2_key, from, subject, message_id, received_at }
4.  Queue Consumer Worker:
    a. Picks up batch of messages
    b. POSTs each to FastAPI: POST /internal/email-received
    c. ACKs on 200, retries on failure (up to 5 times)
5.  FastAPI /internal/email-received:
    a. Validates X-Internal-Secret header
    b. Creates ImportedEmail record (status=received)
    c. Returns 200 immediately
    d. Launches background task: process_email(email_id)
6.  process_email() (async, background):
    a. Lookup alias_hash → external_user_id in aliases table
    b. Fetch .eml from R2
    c. Parse MIME: text body, HTML body, headers, attachments
    d. Classify: check sender domain against financial_sender_registry
              and subject against financial_subject_patterns
    e. If not financial → mark status=non_financial, stop
    f. Extract: try sender template → fall back to general regex
    g. If extraction_confidence < threshold → mark status=extraction_failed
    h. If extraction succeeded:
       - Normalize merchant name (rules engine)
       - Suggest category (lookup table)
       - Compute confidence scores
       - Run duplicate detection
       - Create ExtractionResult (status=pending_review)
    i. POST webhook to budgeting app:
       - extraction.created OR extraction.failed
7.  Budgeting App:
    a. Receives webhook
    b. Shows user "New transaction to review"
    c. User reviews, confirms category
    d. Budgeting app: POST /extractions/{id}/confirm
    e. Budgeting app stores own Transaction record
    f. Budgeting app: POST /feedback/category (updates email API heuristics)
```

## Request Flow: Alias Creation

```
1.  Budgeting app: POST /api/v1/aliases {external_user_id: "user-123"}
2.  FastAPI generates alias_hash (8-char hex, unique)
3.  Creates alias record: { alias_hash: "abc12345", external_user_id: "user-123" }
4.  Returns: { alias: "abc12345@fintrack.raksimoni.com" }
5.  No Cloudflare API call needed — catch-all Worker handles all @fintrack.raksimoni.com
```

## Architecture Principles

1. **Cloudflare handles email transport; Python handles email processing.** The Email Worker is a thin TypeScript shim — store bytes, push to queue. All intelligence (parsing, classification, extraction) is in Python/FastAPI.

2. **No wrong data over partial data.** If extraction confidence is below threshold, mark as failed and surface to user. Never submit a guess as a fact.

3. **This API has no opinion about budgets.** It knows about emails and transactions. Categories are suggested, not assigned. The budgeting app owns the final category.

4. **External_user_id as the user reference.** This API never stores full user profiles. It stores an `external_user_id` that the budgeting app provides. This decouples the two systems and avoids duplicating user management.

5. **Idempotent processing.** `message_id` deduplication prevents the same email being processed twice if the Queue Consumer retries.

6. **Audit trail at every step.** Every status transition (received → classified → extracted → pending_review → confirmed) is logged in `import_audit_logs`.

## Data Flow Summary

```
Raw .eml (Cloudflare R2)
  └─→ MIME parsed (FastAPI)
      └─→ email_classifications (PostgreSQL)
          └─→ extraction_results (PostgreSQL)
              └─→ webhook → Budgeting App
                  └─→ Budgeting App: confirmed Transaction (Budgeting App DB)
                      └─→ POST /feedback/category → email API heuristics updated
```

Raw email content in R2 is the only copy of the full email. PostgreSQL holds structured metadata only. R2 objects are deleted after retention period (default 90 days) while PostgreSQL records are retained for the audit trail.

---

*Cross-references:*
- *Stack details: [architecture/stack-decisions.md](stack-decisions.md)*
- *Redesign rationale: [architecture/redesign-summary.md](redesign-summary.md)*
- *Cloudflare setup: [ingestion/cloudflare-email-setup.md](../ingestion/cloudflare-email-setup.md)*
- *Integration contract: [integration/budgeting-app-integration.md](../integration/budgeting-app-integration.md)*
- *Module details: [architecture/system-modules.md](system-modules.md)*
