# MVP Recommendation

> Updated 2026-05-17 following architectural redesign. See [architecture/redesign-summary.md](../architecture/redesign-summary.md).

## Core Scope: Forwarded Email Only

Inbox OAuth (Gmail/Outlook scanning) is **not in scope**. The MVP handles one ingestion path: users forward financial emails to a unique Cloudflare alias. This keeps the infrastructure simple and avoids OAuth complexity entirely.

**This API is a backend service.** The budgeting app owns all user-facing UI. No frontend is built here.

---

## MVP Architecture

```
Budgeting App → POST /aliases      → create alias (db only, catch-all Cloudflare rule handles routing)
User forwards email to alias        → Cloudflare Email Worker → R2 + Queue
Cloudflare Consumer Worker          → POST /internal/email-received → FastAPI
FastAPI BackgroundTask              → parse → classify → extract → store → webhook
Budgeting App webhook               → new extraction result available
```

---

## MVP Build Checklist

### Phase 1a: Cloudflare Infrastructure (Week 1)

- [ ] Enable Cloudflare Email Routing on domain
- [ ] Write and deploy Cloudflare Email Worker:
  - Receive MIME email
  - Store raw .eml to R2 (`emails/{alias_hash}/{email_id}.eml`)
  - Push to Cloudflare Queue
- [ ] Write and deploy Cloudflare Queue Consumer Worker:
  - Pull from queue
  - POST to FastAPI `/internal/email-received`
  - ACK on 200, retry on failure
- [ ] Create Cloudflare Queue: `email-processing` + DLQ `email-processing-dlq`
- [ ] Test: forward a real email, confirm it lands in R2

### Phase 1b: FastAPI Foundation (Week 1)

- [ ] Project setup: FastAPI, SQLAlchemy async, Alembic, Pydantic v2
- [ ] Database schema migrations (aliases, imported_emails, email_classifications, extraction_results, extraction_snippets, extraction_templates, merchant_rules, category_rules, duplicate_matches, audit_logs)
- [ ] API key authentication middleware
- [ ] Alias management endpoints:
  - `POST /api/v1/aliases`
  - `GET /api/v1/aliases?external_user_id=`
  - `DELETE /api/v1/aliases/{id}`
- [ ] Internal webhook endpoint: `POST /internal/email-received`
- [ ] R2 client (boto3 with Cloudflare R2 credentials)

### Phase 1c: Processing Pipeline (Week 2)

- [ ] MIME parsing (Python `email` library + `html2text`)
- [ ] Email classification (rule-based: sender domain registry + subject keywords)
- [ ] Template extraction for 20 top senders (see list below)
- [ ] General regex extraction (amount, date, card suffix, currency)
- [ ] Merchant normalization
- [ ] Category suggestion (lookup table: merchant → category)
- [ ] Confidence scoring
- [ ] Extraction result storage
- [ ] Extraction failure handling (mark + webhook)

### Phase 1d: Extraction Results API (Week 3)

- [ ] `GET /api/v1/extractions?external_user_id=&status=`
- [ ] `GET /api/v1/extractions/{id}`
- [ ] `GET /api/v1/extractions/{id}/preview`
- [ ] `POST /api/v1/extractions/{id}/confirm`
- [ ] `POST /api/v1/extractions/{id}/dismiss`
- [ ] `POST /api/v1/feedback/category`

### Phase 1e: Duplicate Detection (Week 3)

- [ ] Fingerprint generation: SHA-256(normalized_amount + normalized_merchant + date)
- [ ] Exact fingerprint match (same email forwarded twice, bank alert + receipt on same day)
- [ ] `duplicate_confidence` field on ExtractionResult

### Phase 1f: Outbound Webhook (Week 3)

- [ ] `POST /api/v1/config/webhook` (budgeting app sets webhook URL + secret)
- [ ] Deliver webhook on: extraction created, extraction failed
- [ ] HMAC-SHA256 + timestamp signing
- [ ] Retry: 5 attempts (immediate, 1m, 5m, 15m, 1h)

### Phase 1g: Privacy Basics (Week 4)

- [ ] `DELETE /api/v1/users/{external_user_id}/data` (deactivate aliases + schedule deletion)
- [ ] R2 object retention: delete raw .eml after 90 days (scheduled job via Cloudflare Cron Trigger)

### Phase 1h: Deployment + Hardening (Week 4)

- [ ] Railway deployment (FastAPI + PostgreSQL)
- [ ] Wrangler deploy for Cloudflare Workers
- [ ] Sentry integration (FastAPI + Worker errors)
- [ ] Internal secret rotation mechanism
- [ ] End-to-end test: forward real emails from 5 senders, confirm extraction accuracy

---

## MVP Template Coverage (Top 20 Senders)

Build templates for these senders in MVP. They cover the majority of financial emails for most users.

| Sender | Email Type | Priority |
|--------|-----------|---------|
| amazon.com | Merchant receipt | P0 |
| paypal.com | Payment confirmation | P0 |
| stripe.com | Invoice | P0 |
| apple.com | App Store receipt | P0 |
| google.com | Play Store / Google receipt | P0 |
| uber.com | Ride receipt | P0 |
| chase.com | Bank / CC alert | P0 |
| bankofamerica.com | Bank alert | P0 |
| wellsfargo.com | Bank alert | P0 |
| netflix.com | Subscription invoice | P1 |
| spotify.com | Subscription invoice | P1 |
| doordash.com | Food delivery receipt | P1 |
| venmo.com | Payment notification | P1 |
| cashapp.com | Payment notification | P1 |
| airbnb.com | Booking receipt | P1 |
| lyft.com | Ride receipt | P1 |
| instacart.com | Grocery receipt | P1 |
| shopify.com | Merchant order confirmation | P2 |
| squareup.com | Square receipt | P2 |
| etsy.com | Purchase receipt | P2 |

---

## MVP Confidence Strategy

In MVP, three confidence levels determine what happens to an extraction:

| Confidence | Threshold | Status | Budgeting App Sees |
|-----------|-----------|--------|-------------------|
| High | ≥ 0.85 | `pending_review` | Extraction ready for review |
| Medium | 0.60–0.84 | `pending_review` | Extraction ready, low confidence badge |
| Low / Failed | < 0.60 or required field missing | `extraction_failed` | "Couldn't read this email" notification |

**No auto-approve in MVP.** All extractions require user confirmation in the budgeting app.

**No AI in MVP.** If template + regex both fail → mark as `extraction_failed`. Track failure rate by sender; add templates for top failing senders.

---

## What Is NOT in MVP

| Feature | Phase |
|---------|-------|
| Inbox OAuth (Gmail, Outlook scanning) | Phase 3 |
| AI-based extraction | Phase 2 |
| Fuzzy duplicate detection (pg_trgm) | Phase 2 |
| Multiple aliases per user | Phase 2 |
| Re-extraction endpoint | Phase 2 |
| User-defined merchant rules (via budgeting app) | Phase 2 |
| Category taxonomy sync with budgeting app | Phase 2 |
| Full GDPR data export | Phase 2 |
| Admin extraction analytics | Phase 2 |
| OCR for image/PDF attachments | Phase 3 |

---

## Success Metrics for MVP Launch

| Metric | Target |
|--------|--------|
| Extraction success rate on top-20 senders | ≥ 95% |
| End-to-end processing time (email received → webhook sent) | < 10 seconds |
| False positives (non-financial email classified as financial) | < 2% |
| Duplicate suppression precision (no false duplicates) | > 99% |
| Webhook delivery success rate | > 99% (within 1 hour with retries) |

---

## Build Order Rationale

Build Cloudflare infrastructure first because:
1. You can verify emails land in R2 before writing any Python
2. It validates the full ingestion pipeline independently from the processing pipeline
3. The internal webhook gives you a clean test seam — you can fire it manually during FastAPI development without needing real Cloudflare events

---

*See [architecture/redesign-summary.md](../architecture/redesign-summary.md) for why these choices were made.*
*See [integration/budgeting-app-integration.md](../integration/budgeting-app-integration.md) for the budgeting app contract.*
*See [future-roadmap/roadmap.md](../future-roadmap/roadmap.md) for what comes after MVP.*
