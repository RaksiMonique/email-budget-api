# API Skeleton — REST Endpoint Contracts

> Updated 2026-05-17. This API serves the budgeting app (service-to-service). No user-facing endpoints — the budgeting app proxies all user interactions.

## Base URL
```
https://api.emailbudget.io/api/v1
```

## Authentication

All `/api/v1/` endpoints require:
```
X-API-Key: <service_api_key>
```

Internal webhook endpoints use a separate secret:
```
X-Internal-Secret: <internal_secret>    (Cloudflare Worker → FastAPI)
```

Outbound webhooks to budgeting app are signed with:
```
X-EmailBudget-Signature: sha256=<hmac_hex>
X-EmailBudget-Timestamp: <unix_timestamp>
```

---

## Aliases

### POST /aliases
Create a forwarding alias for a user.

**Body:**
```json
{
  "external_user_id": "budgeting-app-user-uuid",
  "label": "Main inbox"
}
```

**Response 201:**
```json
{
  "id": "uuid",
  "alias": "abc12345@fintrack.raksimoni.com",
  "alias_hash": "abc12345",
  "external_user_id": "budgeting-app-user-uuid",
  "label": "Main inbox",
  "is_active": true,
  "emails_received": 0,
  "created_at": "2026-05-17T10:00:00Z"
}
```

---

### GET /aliases
List aliases for a user.

**Query params:** `external_user_id` (required)

**Response 200:**
```json
{
  "aliases": [
    {
      "id": "uuid",
      "alias": "abc12345@fintrack.raksimoni.com",
      "label": "Main inbox",
      "is_active": true,
      "emails_received": 47,
      "created_at": "2026-05-17T10:00:00Z"
    }
  ]
}
```

---

### DELETE /aliases/{id}
Deactivate an alias. Future emails sent to this alias are silently rejected.

**Response 200:**
```json
{
  "id": "uuid",
  "alias": "abc12345@fintrack.raksimoni.com",
  "is_active": false,
  "deactivated_at": "2026-05-17T11:00:00Z"
}
```

---

## Extraction Results

### GET /extractions
List extraction results for a user.

**Query params:**
- `external_user_id` (required)
- `status`: `pending_review` | `confirmed` | `dismissed` | `extraction_failed` | `non_financial`
- `from_date`, `to_date`
- `page`, `per_page` (default 25, max 100)

**Response 200:**
```json
{
  "extractions": [
    {
      "id": "uuid",
      "external_user_id": "...",
      "alias": "abc12345@fintrack.raksimoni.com",
      "merchant": "Amazon",
      "amount": "45.99",
      "currency": "USD",
      "transaction_date": "2026-05-17",
      "card_suffix": "1234",
      "payment_method": "Visa",
      "category_suggestion": "shopping",
      "transaction_type": "debit",
      "extraction_confidence": 0.94,
      "duplicate_confidence": 0.0,
      "status": "pending_review",
      "email": {
        "from": "receipts@amazon.com",
        "subject": "Your Amazon.com order",
        "received_at": "2026-05-17T09:15:00Z"
      },
      "created_at": "2026-05-17T09:15:30Z"
    }
  ],
  "total": 18,
  "page": 1,
  "per_page": 25
}
```

---

### GET /extractions/{id}
Get full extraction detail.

**Response 200:**
```json
{
  "id": "uuid",
  "external_user_id": "...",
  "alias": "abc12345@fintrack.raksimoni.com",
  "merchant": "Amazon",
  "merchant_normalized": "Amazon",
  "amount": "45.99",
  "currency": "USD",
  "transaction_date": "2026-05-17",
  "email_received_at": "2026-05-17T09:15:00Z",
  "card_suffix": "1234",
  "account_reference": null,
  "payment_method": "Visa",
  "sender_address": "receipts@amazon.com",
  "subject": "Your Amazon.com order #123-456",
  "category_suggestion": "shopping",
  "transaction_type": "debit",
  "extraction_confidence": 0.94,
  "duplicate_confidence": 0.0,
  "extraction_method": "template",
  "field_confidences": {
    "amount": 0.99,
    "merchant": 0.97,
    "transaction_date": 0.95,
    "card_suffix": 0.92,
    "currency": 0.99
  },
  "status": "pending_review",
  "duplicate_matches": [],
  "created_at": "2026-05-17T09:15:30Z"
}
```

---

### GET /extractions/{id}/preview
Return extracted fields alongside the raw text snippet used for extraction. Used by budgeting app to show users "we extracted X from this text".

**Response 200:**
```json
{
  "id": "uuid",
  "fields": {
    "merchant": "Amazon",
    "amount": "45.99",
    "transaction_date": "2026-05-17"
  },
  "raw_snippet": "Order Total: $45.99\nVisa ending in 1234\nOrder Placed: May 17, 2026",
  "sender": "receipts@amazon.com",
  "subject": "Your Amazon.com order #123-456"
}
```

---

### POST /extractions/{id}/confirm
Mark an extraction as confirmed by the user (via budgeting app review flow).

**Body:**
```json
{
  "category": "shopping",
  "notes": "Birthday gift"
}
```

**Response 200:**
```json
{
  "id": "uuid",
  "status": "confirmed",
  "confirmed_at": "2026-05-17T10:30:00Z"
}
```

Idempotent — calling twice returns the same result.

---

### POST /extractions/{id}/dismiss
User dismissed this extraction (not relevant, already logged elsewhere, etc.).

**Body:**
```json
{
  "reason": "already_logged" | "not_mine" | "duplicate" | "other"
}
```

**Response 200:**
```json
{
  "id": "uuid",
  "status": "dismissed"
}
```

---

### POST /extractions/{id}/reprocess
Re-run extraction on a stored email (e.g., after improving a template for this sender).

**Response 202:**
```json
{
  "id": "uuid",
  "status": "reprocessing",
  "previous_confidence": 0.45,
  "previous_status": "pending_review"
}
```

---

## Feedback

### POST /feedback/category
Send a confirmed category assignment back to the Email API for heuristic learning.

**Body:**
```json
{
  "extraction_id": "uuid",
  "merchant_normalized": "Amazon",
  "category_confirmed": "shopping"
}
```

**Response 200:**
```json
{
  "received": true,
  "rule_updated": false,
  "current_rule": null,
  "confirmation_count_for_merchant": 1
}
```

`rule_updated: true` when the confirmation count crosses the threshold to update the merchant-to-category rule (e.g., 3 confirmations → rule).

---

## Stats

### GET /stats/extraction
Extraction accuracy stats for a user.

**Query params:** `external_user_id` (required)

**Response 200:**
```json
{
  "external_user_id": "...",
  "total_emails_received": 142,
  "total_financial": 118,
  "total_extracted_successfully": 109,
  "total_extraction_failed": 9,
  "total_confirmed": 87,
  "total_dismissed": 22,
  "extraction_success_rate": 0.924,
  "average_confidence": 0.89,
  "top_senders": [
    {"sender": "receipts@amazon.com", "count": 23, "success_rate": 1.0},
    {"sender": "alerts@chase.com", "count": 18, "success_rate": 0.94}
  ],
  "top_failing_senders": [
    {"sender": "billing@unknownco.io", "count": 4, "failure_reason": "no_amount_found"}
  ]
}
```

---

## Configuration (per-budgeting-app-installation)

### POST /config/webhook
Set the outbound webhook URL and signing secret.

**Body:**
```json
{
  "webhook_url": "https://yourbudgetapp.com/webhooks/email-extractions",
  "webhook_secret": "whsec_your_secret_here"
}
```

**Response 200:**
```json
{
  "webhook_url": "https://yourbudgetapp.com/webhooks/email-extractions",
  "configured_at": "2026-05-17T10:00:00Z"
}
```

---

### POST /config/webhook/test
Send a test webhook payload to verify the endpoint is working.

**Response 200:**
```json
{
  "delivered": true,
  "status_code": 200,
  "latency_ms": 245
}
```

---

### POST /config/categories
(Phase 2) Send the budgeting app's category taxonomy so the Email API can suggest using real category IDs.

**Body:**
```json
{
  "categories": [
    {"id": "cat_001", "name": "Eating Out", "keywords": ["restaurant", "food", "dining"]},
    {"id": "cat_002", "name": "Groceries", "keywords": ["grocery", "supermarket"]},
    {"id": "cat_003", "name": "Shopping", "keywords": ["amazon", "online", "retail"]}
  ]
}
```

---

## User Data / Privacy

### DELETE /users/{external_user_id}/data
Deactivate all aliases and schedule full data deletion (30-day grace period).

**Response 202:**
```json
{
  "external_user_id": "...",
  "aliases_deactivated": 1,
  "emails_scheduled_for_deletion": 142,
  "scheduled_deletion_date": "2026-06-17T00:00:00Z"
}
```

---

### GET /users/{external_user_id}/data-export
Request a GDPR data export. Returns download URL when ready.

**Response 202:**
```json
{
  "job_id": "uuid",
  "status": "processing",
  "estimated_completion_minutes": 5
}
```

Budgeting app polls `GET /users/{id}/data-export/{job_id}` for completion, then receives a signed R2 download URL.

---

## Internal Endpoints (Not for Budgeting App)

### POST /internal/email-received
Called by Cloudflare Queue Consumer Worker when a new email is ready to process.

**Headers:** `X-Internal-Secret: <secret>`

**Body:**
```json
{
  "email_id": "uuid",
  "alias_hash": "abc12345",
  "r2_key": "emails/abc12345/uuid.eml",
  "from": "receipts@amazon.com",
  "to": "abc12345@fintrack.raksimoni.com",
  "subject": "Your Amazon.com order",
  "message_id": "<uuid@mail.amazon.com>",
  "date_header": "Sat, 17 May 2026 09:15:00 +0000",
  "received_at": "2026-05-17T09:15:00Z"
}
```

**Response 200:** (always, to ensure Consumer Worker ACKs immediately)
```json
{ "received": true }
```

---

### GET /internal/aliases/{alias_hash}
Used by Cloudflare Email Worker to validate alias exists before storing email.

**Headers:** `X-Internal-Secret: <secret>`

**Response 200:**
```json
{ "alias_hash": "abc12345", "external_user_id": "user-uuid", "is_active": true }
```

**Response 404:**
```json
{ "exists": false }
```

---

## Health

### GET /health
No auth required.

**Response 200:**
```json
{
  "status": "healthy",
  "db": "ok",
  "r2": "ok",
  "version": "1.0.0"
}
```

---

## Outbound Webhook Events (Email API → Budgeting App)

### extraction.created
```json
{
  "event": "extraction.created",
  "id": "evt_uuid",
  "api_version": "2026-05-01",
  "created_at": "2026-05-17T09:15:30Z",
  "data": {
    "extraction_id": "uuid",
    "external_user_id": "...",
    "alias": "abc12345@fintrack.raksimoni.com",
    "merchant": "Amazon",
    "amount": "45.99",
    "currency": "USD",
    "transaction_date": "2026-05-17",
    "category_suggestion": "shopping",
    "extraction_confidence": 0.94,
    "duplicate_confidence": 0.0,
    "status": "pending_review",
    "email": {
      "from": "receipts@amazon.com",
      "subject": "Your Amazon.com order #123",
      "received_at": "2026-05-17T09:15:00Z"
    }
  }
}
```

### extraction.failed
```json
{
  "event": "extraction.failed",
  "id": "evt_uuid",
  "created_at": "2026-05-17T09:15:30Z",
  "data": {
    "extraction_id": "uuid",
    "external_user_id": "...",
    "failure_reason": "no_amount_found",
    "email": {
      "from": "billing@unknownco.io",
      "subject": "Invoice #12345",
      "received_at": "2026-05-17T09:15:00Z"
    },
    "preview_url": "https://api.emailbudget.io/api/v1/extractions/uuid/preview"
  }
}
```

**Webhook delivery:**
- Signed with `X-EmailBudget-Signature: sha256=<hmac>` and `X-EmailBudget-Timestamp`
- Retry: 5 attempts (immediate, 1m, 5m, 15m, 1h)
- See [api/webhook-strategy.md](webhook-strategy.md) for verification code

---

*See [integration/budgeting-app-integration.md](../integration/budgeting-app-integration.md) for the full integration guide.*
*See [api/webhook-strategy.md](webhook-strategy.md) for webhook security details.*
