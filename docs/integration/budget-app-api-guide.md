# Email Budget API — Integration Guide

**For the budgeting-app team.** How to connect to the Email Budget API, what your
app must set up, and the exact request/response shapes. All examples below are
**real** responses from the live service.

---

## What it does

The Email Budget API turns a user's **forwarded bank / receipt emails** into
structured, reviewable transactions. Your budgeting app is its **only client** —
you call it on behalf of your users. It never talks to users directly and knows
nothing about "budgets."

```
User ──▶ Budgeting App ──▶ Email Budget API ──▶ Cloudflare (email, queue, storage)
                    ◀── extractions (pull or webhook) ──
```

Every parsed transaction starts as **`pending_review`** — the API **never
auto-confirms**. Your user reviews it, and you confirm it. Missing fields are left
blank, never guessed.

---

## 1. Connect

| | |
|---|---|
| **Base URL** | `https://email-budget-api.onrender.com` |
| **Auth** | Header `X-API-Key: <key>` on **every** `/api/v1/*` request |
| **Content type** | `application/json` |

- **Get the key** from the API owner (one key per environment — dev/prod).
- **Cold start:** the service is on a free tier; the first request after ~15 min
  idle takes **~50s** while it wakes. Use generous timeouts and retry.
- **Connectivity check:**
  ```bash
  curl -H "X-API-Key: $KEY" \
    "https://email-budget-api.onrender.com/api/v1/extractions?external_user_id=demo-user"
  ```
  A `200` with a JSON page (even empty) means you're connected. `401` = bad/missing key.

---

## 2. Core concepts

- **`external_user_id`** — *your* identifier for a user (any string, ≤255 chars).
  You choose it; the API stores it verbatim and **scopes everything by it**. Use
  your stable internal user id. The API never sees your users otherwise.
- **alias** — a unique forwarding address for one user, e.g.
  `ab12cd34ef56gh78@fintrack.raksimoni.com`. Email forwarded there is processed
  for that user.
- **extraction** — one parsed transaction. Lifecycle:
  `pending_review → confirmed` (user accepted) or `→ dismissed` (user rejected).
  `extraction_failed` = an email arrived but no amount could be parsed.
- **Money & confidence are STRINGS** (`"amount": "18.99"`). Parse as decimal —
  **never** as a float.

---

## 3. Setup checklist (what your app must build)

1. **Store the API key** server-side (never ship it to the client).
2. **On user onboarding** (email feature enabled): create an alias + show the user
   how to forward.
3. **Consume extractions** — pull now, add the webhook later.
4. **Review UI** — show pending items → user confirms/dismisses → send feedback.
5. **On account deletion** — call the deletion endpoint.

---

## 4. Onboard a user — create an alias

```
POST /api/v1/aliases
X-API-Key: <key>
{ "external_user_id": "your-user-id" }
```
**201 response:**
```json
{
  "id": "…",
  "alias_hash": "ab12cd34ef56gh78",
  "email_address": "ab12cd34ef56gh78@fintrack.raksimoni.com",
  "external_user_id": "your-user-id",
  "is_active": true,
  "emails_received": 0
}
```

- Show the user how to forward to `email_address`. The intended path is a
  **server-side auto-forward rule** (Gmail filter → the alias; Outlook rule) so
  every bank alert flows in automatically. A manual one-off "Fwd" also works but is
  best-effort.
- **Confirm forwarding works:** poll `GET /api/v1/aliases?external_user_id=…` until
  `emails_received > 0`, or subscribe to the `alias.first_email_received` webhook.

---

## 5. Get transactions — two ways

### A) Pull (works immediately)

```
GET /api/v1/extractions?external_user_id=<id>&status=pending_review&limit=50&offset=0
X-API-Key: <key>
```
**200 response** (`ExtractionPage`):
```json
{
  "items": [
    { "id": "11111111-1111-1111-1111-111111111111", "merchant_normalized": "Corner Cafe Kingston",
      "amount": "1250.00", "currency": "JMD", "transaction_date": "2026-05-01",
      "status": "pending_review", "confidence_band": "high", "duplicate_confidence": "0" }
  ],
  "total": 6, "limit": 50, "offset": 0
}
```
Query params: `external_user_id` (**required**), `status` (optional:
`pending_review` | `confirmed` | `dismissed` | `extraction_failed`), `limit`
(1–200, default 50), `offset`.

### B) Push (webhook — recommended once you have a receiver)

```
POST /api/v1/config/webhook
X-API-Key: <key>
{ "webhook_url": "https://your-app/webhooks/email-extractions",
  "webhook_secret": "<a strong shared secret, ≥16 chars>" }
```
Then every new extraction POSTs to your URL (see **§8**). Smoke-test the wire:
```
POST /api/v1/config/webhook/test   →  { "delivered": true, "status_code": 200 }
```
> ⚠️ Once configured, **already-queued** events deliver on the next poll — real
> financial data reaches whatever environment you pointed at. Treat dev data as real.

---

## 6. Review flow

1. Show the user their `pending_review` items.
2. On open, get full detail: `GET /api/v1/extractions/{id}` (or `…/preview` to also
   get the raw email snippet each field came from).
3. **Confirm:** `POST /api/v1/extractions/{id}/confirm` body `{ "category": "food_and_dining" }`
   (category optional) → row becomes `confirmed`. Idempotent.
   **Dismiss:** `POST /api/v1/extractions/{id}/dismiss` body `{ "reason": "not mine" }`
   (reason optional) → row becomes `dismissed`.
4. **Teach it:** `POST /api/v1/feedback/category`
   `{ "merchant_normalized": "Amazon", "category_confirmed": "shopping", "extraction_id": "…" }`
   After the **same** merchant→category is confirmed **3×**, the API auto-suggests
   that category for that merchant going forward.
5. Store the returned `extraction_id` on your own Transaction record (for linking
   and to avoid re-importing).

---

## 7. Data shapes

**Extraction detail** (`GET /api/v1/extractions/{id}`) — real example:
```json
{
  "id": "11111111-1111-1111-1111-111111111111",
  "email_id": "22222222-2222-2222-2222-222222222222",
  "external_user_id": "user-1234",
  "amount": "1250.00",              // STRING; null if not found
  "currency": "JMD",                // null if unknown
  "merchant_normalized": "Corner Cafe Kingston",
  "merchant_raw": "CORNER CAFE KINGSTON",
  "category_suggestion": null,      // string or null
  "category_confirmed": null,       // set once the user confirms
  "transaction_date": "2026-05-01", // ISO date, or null
  "card_last4": "4821",             // or null
  "extraction_confidence": "0.921", // STRING 0–1
  "confidence_band": "high",        // "high" | "low_confidence"
  "duplicate_confidence": "0",      // "1" ⇒ exact-match exists, show a badge
  "status": "pending_review",
  "method": "template",             // template | regex | default
  "fingerprint": "a1b2c3d4e5f6…",
  "dismissed_reason": null,
  "field_confidences": { "amount": 0.97, "merchant": 0.97, "…": 0.97 },
  "duplicate_matches": [],
  "created_at": "2026-05-01T14:03:00Z"
}
```
Field notes:
- **`status`** — `pending_review` | `confirmed` | `dismissed` | `extraction_failed`.
- **`confidence_band`** — `high` (complete + confident) or `low_confidence` (usable
  but review closely). Both are still `pending_review`.
- **`duplicate_confidence: "1"`** — an exact same-transaction match already exists.
  The row is **still live** (never auto-suppressed) — show a "possible duplicate"
  badge and let the user decide.
- **currency** — a bare `$` from a Jamaican bank defaults to `JMD`; the user can
  correct it on review.

---

## 8. Webhook payload + signature

Every delivery is compact JSON `{ event, event_id, created_at, data }`:
```json
{
  "event": "extraction.created",
  "event_id": "outbox-row-uuid",
  "created_at": "2026-08-10T22:20:00+00:00",
  "data": { /* same fields as the extraction detail above */ }
}
```
**Events:** `extraction.created`, `extraction.failed` (same `data` shape, money/date
null), `alias.first_email_received`, `forwarding.verification`.

**Headers:** `X-EmailBudget-Timestamp: <unix seconds>` and
`X-EmailBudget-Signature: <hex>` — a **raw** HMAC-SHA256 hex digest, **no `sha256=`
prefix**. Verify over the **raw request body bytes** (re-serializing changes the
bytes and breaks it):
```python
import hmac, hashlib, time

def verify(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 300:       # 5-min replay window
        return False
    msg = f"{timestamp}.".encode() + body             # sign over "{ts}." + raw body
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)   # raw hex, no prefix
```
Return `2xx` to ack. Non-2xx / timeouts are retried with backoff.

---

## 9. Endpoint reference

All under `https://email-budget-api.onrender.com`, all require `X-API-Key`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/aliases` | Create a user's forwarding alias |
| `GET` | `/api/v1/aliases?external_user_id=` | List a user's aliases |
| `DELETE` | `/api/v1/aliases/{id}` | Deactivate an alias |
| `GET` | `/api/v1/extractions?external_user_id=&status=&limit=&offset=` | List transactions (page) |
| `GET` | `/api/v1/extractions/{id}` | Full extraction detail |
| `GET` | `/api/v1/extractions/{id}/preview` | Detail + raw email snippets |
| `POST` | `/api/v1/extractions/{id}/confirm` | User confirmed → `confirmed` |
| `POST` | `/api/v1/extractions/{id}/dismiss` | User rejected → `dismissed` |
| `POST` | `/api/v1/extractions/{id}/reprocess` | Re-run extraction on the stored email |
| `POST` | `/api/v1/feedback/category` | Teach merchant→category |
| `GET` | `/api/v1/stats/extraction?external_user_id=` | Per-user extraction stats |
| `POST` | `/api/v1/config/webhook` | Register your webhook URL + secret |
| `POST` | `/api/v1/config/webhook/test` | Send a signed test event |
| `DELETE` | `/api/v1/users/{external_user_id}/data` | Schedule user data deletion (GDPR) |

---

## 10. Things to handle

- **Cold start** (~50s first request after idle) — generous timeouts + retry.
- **Decimals as strings** — parse `amount`/confidences as decimals, never floats.
- **Human-in-the-loop** — everything is `pending_review`; nothing is auto-booked.
  Your UI drives confirm/dismiss.
- **`extraction_failed`** — an email arrived but couldn't be parsed (e.g. no amount).
  Surface it as "couldn't read this one — enter manually," don't drop it silently.
- **Duplicates** — `duplicate_confidence: "1"` ⇒ show a badge; the row is still live.
- **Idempotency** — `confirm` is safe to call twice. Store `extraction_id` on your
  side to avoid re-importing.

---

*Questions or a field that doesn't match? The source of truth is the deployed
service — ping the API owner. This guide is generated from the live schemas.*
