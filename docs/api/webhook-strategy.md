# Webhook Strategy

> ⚠️ **PARTIALLY SUPERSEDED (pre-redesign).** The inbound half (Postmark Inbound Parse, Celery, Redis dedup) is removed — ingestion is now Cloudflare Email Routing → Worker → Queue → `POST /internal/email-received` ([cloudflare-email-setup.md](../ingestion/cloudflare-email-setup.md)). The outbound webhook is now the **outbox pattern** in [PLAN.md](../../PLAN.md) Phase 7 (HMAC-SHA256 signing carries over). Treat those as authoritative.

## Overview

The system handles three distinct webhook categories:
1. **Inbound webhooks** — receiving events from external providers (Postmark, Nylas)
2. **Outbound webhooks** — delivering approved transactions to the budget app
3. **Internal event hooks** — future extensibility (not in MVP)

---

## 1. Inbound Webhook: Postmark Inbound Parse

**Direction:** Postmark → This API

**Endpoint:** `POST /webhooks/inbound-email`

**Purpose:** Receive forwarded financial emails from users.

### Authentication
Postmark includes a shared token in every inbound webhook request header. The token is set in the Postmark dashboard and stored as an environment variable.

```
Header: X-Postmark-Inbound-Token: <shared_token>
```

Verification:
```python
def verify_postmark_token(request: Request) -> bool:
    provided = request.headers.get("X-Postmark-Inbound-Token", "")
    expected = settings.POSTMARK_INBOUND_TOKEN
    return secrets.compare_digest(provided, expected)
```

### Reliability Contract
- Always return `HTTP 200` regardless of processing outcome
- Postmark will retry on any non-2xx response (up to 10 retries over 24 hours)
- Returning 200 does NOT mean processing succeeded — it means the webhook was received
- All processing happens asynchronously in Celery

### Idempotency
Postmark may deliver the same webhook twice in rare cases (network retry after timeout). Guard:
```python
# Check message_id deduplication in Redis before creating ImportedEmail
cache_key = f"webhook_seen:{user_id}:{message_id}"
if await redis.exists(cache_key):
    return {"received": True}  # already handled
await redis.setex(cache_key, 3600, "1")  # 1-hour dedup window
```

### Payload Structure (relevant fields)
```json
{
  "To": "abc12345@fintrack.raksimoni.com",
  "OriginalRecipient": "abc12345@fintrack.raksimoni.com",
  "From": "receipts@amazon.com",
  "FromName": "Amazon",
  "Subject": "Your Amazon.com order",
  "MessageID": "<uuid@mail.amazon.com>",
  "Date": "Tue, 6 May 2026 09:15:00 +0000",
  "TextBody": "Order Total: $45.99...",
  "HtmlBody": "<html>...</html>",
  "StrippedTextReply": "",
  "Headers": [
    {"Name": "X-PM-SpamScore", "Value": "1.2"},
    {"Name": "X-Spam-Status", "Value": "No"},
    {"Name": "DKIM-Signature", "Value": "..."}
  ],
  "Attachments": [
    {
      "Name": "receipt.pdf",
      "ContentType": "application/pdf",
      "ContentLength": 45231,
      "Content": "<base64>"
    }
  ]
}
```

### Response Time Requirement
Postmark expects a response within 30 seconds. The endpoint must:
1. Validate the token (instant)
2. Look up the forwarding address (< 5ms DB index lookup)
3. Check rate limit (< 1ms Redis)
4. Enqueue Celery task (< 5ms Redis push)
5. Return 200

Total synchronous work: < 50ms. Postmark timeout is 30 seconds — well within budget.

---

## 2. Inbound Webhook: Nylas

**Direction:** Nylas → This API

**Endpoint:** `POST /webhooks/nylas`

**Purpose:** Receive real-time notifications when new emails arrive in connected inboxes or when grants expire.

### Authentication
Nylas signs every webhook with HMAC-SHA256 using a webhook secret set in the Nylas dashboard.

```
Header: X-Nylas-Signature: <hmac_sha256_hex>
```

Verification:
```python
def verify_nylas_signature(request: Request, body: bytes) -> bool:
    provided = request.headers.get("X-Nylas-Signature", "")
    expected = hmac.new(
        settings.NYLAS_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    return secrets.compare_digest(provided, expected)
```

### Event Types

| Event | Action |
|-------|--------|
| `message.created` | Fetch message from Nylas, enqueue email pipeline |
| `grant.expired` | Mark InboxConnection as `needs_reauth`, notify user |
| `grant.deleted` | Mark InboxConnection as `revoked` |

### Payload Structure
```json
{
  "specversion": "1.0",
  "type": "message.created",
  "source": "nylas",
  "id": "event-uuid",
  "time": 1746524400,
  "datacontenttype": "application/json",
  "data": {
    "application_id": "app-uuid",
    "object": {
      "type": "message",
      "grant_id": "nylas-grant-uuid",
      "id": "nylas-message-id",
      "date": 1746524400,
      "subject": "Your Amazon.com order",
      "from": [{"email": "receipts@amazon.com", "name": "Amazon"}],
      "to": [{"email": "user@gmail.com", "name": ""}]
    }
  }
}
```

### Reliability
Nylas retries undelivered webhooks for 24 hours. Same pattern as Postmark: always return 200, process async. Include idempotency guard on `event.id`.

---

## 3. Outbound Webhook: Budget App Delivery

**Direction:** This API → Budget App

**Purpose:** Deliver approved transactions to the user's budget app in real time.

### Configuration
Per-user, stored encrypted in `user_preferences`:
- `budget_app_webhook_url`: target URL
- `budget_app_webhook_secret`: HMAC signing secret

### Payload

```json
{
  "event": "transaction.approved",
  "id": "evt_b3d1f42e-...",
  "api_version": "2026-05-01",
  "created_at": "2026-05-06T10:00:00Z",
  "data": {
    "transaction": {
      "id": "uuid",
      "merchant": "Amazon",
      "amount": "45.99",
      "currency": "USD",
      "transaction_date": "2026-05-06",
      "category": "shopping",
      "transaction_type": "debit",
      "payment_method": "Visa",
      "card_suffix": "1234",
      "notes": "",
      "source": "forwarded_email",
      "email_id": "uuid",
      "extraction_confidence": 0.94,
      "approved_at": "2026-05-06T10:00:00Z"
    }
  }
}
```

### Signing

Every outbound webhook request includes two headers:

```
X-EmailBudget-Signature: sha256=<hmac_sha256_hex>
X-EmailBudget-Timestamp: 1746524400
```

Signing process:
```python
def sign_webhook(payload: dict, secret: str, timestamp: int) -> str:
    # Include timestamp in signed payload to prevent replay attacks
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    signed_content = f"{timestamp}.{body}"
    return hmac.new(
        secret.encode(),
        signed_content.encode(),
        hashlib.sha256
    ).hexdigest()
```

**Budget app validation recipe:**
```python
def verify_incoming_webhook(request):
    timestamp = int(request.headers["X-EmailBudget-Timestamp"])
    signature = request.headers["X-EmailBudget-Signature"].removeprefix("sha256=")
    
    # Reject webhooks older than 5 minutes (replay protection)
    if abs(time.time() - timestamp) > 300:
        return False
    
    body = json.dumps(request.json(), separators=(",", ":"), sort_keys=True)
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        f"{timestamp}.{body}".encode(),
        hashlib.sha256
    ).hexdigest()
    
    return secrets.compare_digest(signature, expected)
```

### Delivery Reliability: Outbox Pattern

Webhook delivery uses the **transactional outbox pattern** to guarantee at-least-once delivery:

```
1. ApprovedTransaction created → webhook_delivered=false
2. Celery task: deliver_webhook(transaction_id)
3. POST to budget_app_webhook_url
4. On success: mark webhook_delivered=true, webhook_delivered_at=now()
5. On failure: log attempt, schedule retry
```

Retry schedule:
| Attempt | Delay |
|---------|-------|
| 1st | immediate |
| 2nd | 1 minute |
| 3rd | 5 minutes |
| 4th | 15 minutes |
| 5th | 1 hour |
| Final | Mark as permanently failed, alert user |

```python
# Nightly job: retry any permanently failed deliveries on user action
# User can also manually trigger re-delivery: POST /transactions/{id}/redeliver
```

### Delivery Status

`GET /transactions/{id}` includes delivery status:
```json
{
  "id": "uuid",
  "webhook_delivered": true,
  "webhook_delivered_at": "2026-05-06T10:00:05Z",
  "webhook_attempts": 1
}
```

If delivery has permanently failed:
```json
{
  "webhook_delivered": false,
  "webhook_attempts": 5,
  "webhook_last_error": "Connection refused: https://yourapp.com/webhooks/transactions"
}
```

---

## 4. Clerk Lifecycle Webhooks (Inbound)

**Direction:** Clerk → This API

**Endpoint:** `POST /webhooks/clerk`

**Purpose:** Respond to user lifecycle events.

### Events Handled

| Event | Action |
|-------|--------|
| `user.created` | Provision User record, ForwardingAddress, UserPreferences |
| `user.updated` | Sync email/display_name to User record |
| `user.deleted` | Begin account deletion flow |

### Authentication
Clerk uses `svix` for webhook delivery with HMAC-SHA256 signing:

```python
from svix.webhooks import Webhook

def verify_clerk_webhook(request: Request, body: bytes) -> dict:
    wh = Webhook(settings.CLERK_WEBHOOK_SECRET)
    try:
        return wh.verify(body, dict(request.headers))
    except Exception:
        raise HTTPException(400, "Invalid Clerk webhook signature")
```

---

## Webhook Testing and Development

**Local development:**
```bash
# Expose local FastAPI via ngrok
ngrok http 8000

# Configure Postmark inbound server webhook to your ngrok URL
# https://dashboard.postmark.com/servers/{id}/message-streams/inbound

# Test Postmark webhook manually
curl -X POST http://localhost:8000/webhooks/inbound-email \
  -H "X-Postmark-Inbound-Token: test_token" \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/emails/amazon_receipt_postmark.json
```

**Webhook test endpoint (dev only):**
```
POST /dev/test-webhook?type=inbound_email&fixture=amazon_receipt
```
Only available when `ENVIRONMENT=development`. Fires the full pipeline with a fixture email.

---

## Webhook Security Checklist

- [ ] All inbound webhooks validate provider-specific signatures before processing
- [ ] `secrets.compare_digest` used for all HMAC comparisons (timing-safe)
- [ ] Webhook endpoints always return 200 (never expose 401/403 to webhook providers)
- [ ] Idempotency guards prevent double-processing
- [ ] Outbound webhooks signed with HMAC-SHA256 + timestamp
- [ ] Outbound webhook secret stored encrypted in DB
- [ ] Replay attack protection via timestamp window on outbound webhooks
- [ ] Webhook delivery retried with exponential backoff
- [ ] Failed deliveries surfaced to users

---

*See [ingestion/forwarded-email.md](../ingestion/forwarded-email.md) for Postmark inbound processing details.*
*See [ingestion/inbox-connection.md](../ingestion/inbox-connection.md) for Nylas webhook handling.*
*See [deployment/queue-job-design.md](../deployment/queue-job-design.md) for the `webhooks` Celery queue.*
