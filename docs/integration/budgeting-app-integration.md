# Budgeting App Integration Guide

> **Keep this file when working on the budgeting app.** It defines the full contract between the Email Budget API and the client budgeting application: what each side owns, how they communicate, and what the budgeting app needs to implement.

---

## Overview

The Email Budget API is a standalone backend service. The budgeting app is its only client. Users interact with the budgeting app; the budgeting app calls the Email API on their behalf.

```
User ←→ Budgeting App ←→ Email Budget API ←→ Cloudflare (email, queue, storage)
```

The Email API never talks to users directly. It does not know what a "budget" is.

---

## What the Budgeting App Must Implement

### 1. Alias Provisioning (User Onboarding)

When a user enables the email tracking feature in the budgeting app:

```
POST https://api.emailbudget.io/api/v1/aliases
Headers:
  X-API-Key: <service_api_key>
Body:
  {
    "external_user_id": "budgeting-app-user-uuid",
    "label": "Main inbox"   // optional, shown in settings
  }
Response:
  {
    "alias_id": "uuid",
    "alias": "abc123@fintrack.raksimoni.com",
    "external_user_id": "budgeting-app-user-uuid",
    "created_at": "2026-05-17T10:00:00Z"
  }
```

The budgeting app stores this alias and displays it to the user in settings ("Forward receipts to: abc123@fintrack.raksimoni.com").

### 2. Webhook Endpoint for New Extractions

The budgeting app must expose an HTTPS endpoint that the Email API POSTs to when a new extraction result is ready.

**Configure this in Email API settings:**
```
POST /api/v1/config/webhook
{
  "webhook_url": "https://yourbudgetapp.com/webhooks/email-extractions",
  "webhook_secret": "whsec_..."
}
```

**What the budgeting app receives:**

```json
// New extraction ready for review
{
  "event": "extraction.created",
  "id": "evt_uuid",
  "created_at": "2026-05-17T10:00:00Z",
  "data": {
    "extraction_id": "uuid",
    "external_user_id": "budgeting-app-user-uuid",
    "alias": "abc123@fintrack.raksimoni.com",
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

```json
// Extraction failed — email couldn't be parsed
{
  "event": "extraction.failed",
  "id": "evt_uuid",
  "created_at": "2026-05-17T10:00:00Z",
  "data": {
    "extraction_id": "uuid",
    "external_user_id": "budgeting-app-user-uuid",
    "failure_reason": "no_amount_found",
    "email": {
      "from": "billing@unknownco.io",
      "subject": "Your invoice",
      "received_at": "2026-05-17T09:15:00Z"
    }
  }
}
```

**Verifying webhook signatures:**
```python
import hmac, hashlib, time

def verify_webhook(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:  # 5-minute replay protection
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature)
```

### 3. Transaction Review Flow

When the budgeting app receives an `extraction.created` webhook:

1. Store the `extraction_id` internally (linked to a notification or pending item)
2. Show user a "New transaction to review" notification/badge
3. When user opens it: call `GET /api/v1/extractions/{id}` for full detail
4. Present editable form pre-filled with extracted values
5. User adjusts category, merchant name, notes
6. On confirm:
   ```
   POST /api/v1/extractions/{id}/confirm
   {
     "category": "shopping",          // the category the user confirmed
     "notes": "Birthday gift"
   }
   ```
7. Create the `Transaction` record in the budgeting app's own database
8. Send category feedback:
   ```
   POST /api/v1/feedback/category
   {
     "extraction_id": "uuid",
     "merchant_normalized": "Amazon",
     "category_confirmed": "shopping"
   }
   ```

### 4. Category Taxonomy Alignment

The Email API uses a built-in category vocabulary for suggestions. The budgeting app has user-defined categories. The budgeting app must map between them.

**Email API suggestion vocabulary:**
```
food_and_dining, groceries, transport, entertainment, shopping,
utilities, subscriptions, health, travel, atm_cash, transfers, other
```

**Mapping approach options:**
- A) Budgeting app stores a mapping: `{email_api_suggestion: user_category_id}`
- B) When displaying to user, budgeting app shows its own category list with the email API suggestion pre-selected as the best match
- C) Budgeting app sends its category list to the email API so it can suggest using that taxonomy directly (Phase 2 enhancement)

Option B is simplest for MVP. Option C is the long-term goal.

**Phase 2: Send category taxonomy to Email API**
```
POST /api/v1/config/categories
{
  "categories": [
    {"id": "cat_001", "name": "Eating Out", "keywords": ["restaurant", "food", "dining"]},
    {"id": "cat_002", "name": "Groceries", "keywords": ["grocery", "supermarket"]},
    ...
  ]
}
```
Email API then suggests using the budgeting app's actual category IDs.

### 5. Account Deletion

When a user deletes their budgeting app account or disables the email feature:

```
DELETE /api/v1/users/{external_user_id}/data
Headers:
  X-API-Key: <service_api_key>
  
Response 202:
{
  "scheduled_deletion_date": "2026-06-17T00:00:00Z",
  "aliases_deactivated": 1,
  "emails_to_delete": 142
}
```

This deactivates the user's aliases and schedules all email content for deletion (30-day grace period per GDPR).

---

## What the Budgeting App Must Store

When a user confirms a transaction from an email extraction, the budgeting app creates its own `Transaction` record and should include:

```sql
-- Budgeting app Transaction table (additions for email integration)
ALTER TABLE transactions ADD COLUMN email_extraction_id UUID;        -- Email API's extraction ID
ALTER TABLE transactions ADD COLUMN email_source VARCHAR(50);        -- 'email_forwarded'
ALTER TABLE transactions ADD COLUMN email_from VARCHAR(320);         -- original sender
ALTER TABLE transactions ADD COLUMN email_subject TEXT;              -- original subject
ALTER TABLE transactions ADD COLUMN extraction_confidence DECIMAL;   -- for user transparency
```

This lets the budgeting app:
- Show users "This transaction was imported from email receipts@amazon.com"
- Link back to the raw email preview (via `GET /extractions/{id}/preview`)
- Exclude email-sourced transactions from duplicate detection against manually entered ones

---

## API Endpoint Reference for Budgeting App Developers

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/v1/aliases` | Create forwarding alias for a user |
| `GET` | `/api/v1/aliases?external_user_id=` | List user's aliases |
| `DELETE` | `/api/v1/aliases/{id}` | Deactivate an alias |
| `GET` | `/api/v1/extractions?external_user_id=&status=` | List extraction results |
| `GET` | `/api/v1/extractions/{id}` | Full extraction detail |
| `GET` | `/api/v1/extractions/{id}/preview` | Extracted fields + raw snippet |
| `POST` | `/api/v1/extractions/{id}/confirm` | User confirmed this extraction |
| `POST` | `/api/v1/extractions/{id}/dismiss` | User dismissed/rejected this extraction |
| `POST` | `/api/v1/extractions/{id}/reprocess` | Re-run extraction (after template update) |
| `POST` | `/api/v1/feedback/category` | Send category confirmation for learning |
| `GET` | `/api/v1/stats/extraction?external_user_id=` | Extraction accuracy stats for user |
| `POST` | `/api/v1/config/webhook` | Set webhook URL and secret |
| `POST` | `/api/v1/config/categories` | (Phase 2) Send category taxonomy |
| `DELETE` | `/api/v1/users/{external_user_id}/data` | Initiate user data deletion |
| `GET` | `/api/v1/users/{external_user_id}/data-export` | GDPR data export |

**Authentication:** All requests from the budgeting app use:
```
X-API-Key: <your_service_api_key>
```

---

## Heuristic Feedback Loop — How It Works

The budgeting app participates in improving extraction quality by sending category confirmations back to the Email API.

```
Email API extracts: merchant=Amazon, category_suggestion=shopping
Budgeting app shows user: suggested Shopping (user's category)
User changes to: "Gifts"

Budgeting app → POST /feedback/category
{
  "extraction_id": "uuid",
  "merchant_normalized": "Amazon",
  "category_confirmed": "gifts"     // user's category ID or name
}

Email API:
  - Logs this feedback
  - If same merchant gets "gifts" confirmed 3+ times → update merchant rule
  - Future Amazon extractions → suggest "gifts" (or the most common confirmed category)
```

This is a **pure heuristics approach** — no machine learning. Just a frequency table that the budgeting app contributes to. Over time, suggestions become personalized per-user (or globally across all users for common merchants).

---

## Error States the Budgeting App Must Handle

| Event | Budgeting App Response |
|-------|----------------------|
| `extraction.failed` | Notify user "We received a receipt but couldn't read it — enter manually" |
| `extraction.created` with `duplicate_confidence > 0.8` | Show "Possible duplicate" badge in review UI |
| `extraction.created` with `extraction_confidence < 0.6` | Show "Low confidence" badge, pre-populate fields as editable |
| Webhook delivery failed (Email API retries for 24h) | Email API retries; budgeting app may poll as fallback |
| Alias not found / deactivated | Email API swallows the email; budgeting app should show alias status in settings |

---

## Future Integration Enhancements (Phase 2+)

- **Send budgeting app category taxonomy** so Email API suggests using real category IDs
- **Batch confirmation**: `POST /extractions/bulk-confirm [{id, category}]`
- **User rules API**: Budgeting app surfaces merchant → category rules editor backed by Email API rules
- **Extraction analytics**: Budgeting app dashboard shows "142 transactions auto-imported this month, 94% accuracy"
- **Real-time WebSocket**: Replace webhook polling with WebSocket subscription for instant review notifications
- **OCR for attachments**: Email API extracts from PDF/image receipts; budgeting app gets richer data

---

*This document is the integration contract. Changes to the API contract must be reflected here first, then coordinated with the budgeting app.*
