# Email Budget API — Integration Guide

**The single source of truth** for connecting the budgeting app to the Email Budget
API: how to connect, what your app must set up, and the exact request/response
shapes. Every schema and example below is verified against the **live** service and
uses **fictional** sample data (safe to share).

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
  your stable internal user id.
- **alias** — a unique forwarding address for one user, e.g.
  `ab12cd34ef56gh78@fintrack.raksimoni.com`. Email forwarded there is processed for
  that user.
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
5. **Map categories** — the API suggests from a fixed vocabulary (§7); map to yours.
6. **On account deletion** — call the deletion endpoint (§8).

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

**Onboarding timing — Gmail forwarding confirmation.** When the user adds their
forwarding address, Gmail emails a confirmation to their alias; we detect it and
send you a `forwarding.verification` webhook (§10) carrying the `confirmation_url`
for the user to click.

| Scenario | Time to reach your webhook |
|---|---|
| Normal (warm) | **~15–60s** — mostly Gmail's send + routing; our processing + the 5s delivery poll add ~5–6s |
| Cold start (API idle >15 min) | up to **~1.5 min** — the first request wakes the free-tier instance (~50s); nothing lost |
| Receiver down / erroring | retried at 60s → 5m → 15m → 1h (5 attempts), then `failed` |

Design for it: (1) **register your webhook before onboarding** — `forwarding.verification`
is webhook-only, so the receiver must be live and reliable or the event just waits;
(2) show a "waiting for Gmail confirmation…" state that tolerates ~1–2 min, then
display the `confirmation_url`. Don't assume it's instant.

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
Then every new extraction POSTs to your URL (see **§10**). Smoke-test the wire:
```
POST /api/v1/config/webhook/test   →  { "delivered": true, "status_code": 200 }
```
> ⚠️ Once configured, **already-queued** events deliver on the next poll — real
> financial data reaches whatever environment you pointed at. Treat dev data as real.
>
> ✅ **Per-key routing:** each API key has its **own** webhook (URL + secret) —
> register once per key via `POST /api/v1/config/webhook` (it's scoped to the key in
> your `X-API-Key`). An extraction routes to the webhook of the **key that created
> that user's alias**, so **dev key → dev receiver and prod key → prod receiver run
> simultaneously, no interference**. `/config/webhook/test` tests the calling key's
> receiver. A key with no webhook registered just holds its events (deferred, and
> recoverable via pull) until you register one — it never falls back to another
> key's receiver. **So register a key's webhook before you onboard users on that
> key.**
>
> 🛠 **One-time transition note (per-key deploy, 2026-08-18):** the migration set
> every pre-existing registration's key to NULL (a legacy/global config can't be
> mapped to a key). Keyed events do **not** use a NULL-slot config, so **any client
> that registered *before* the per-key deploy must re-register once** with its key.
> Registrations made after the deploy (including your production webhook) are keyed
> from creation and are unaffected.

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
   `{ "merchant_normalized": "Amazon", "category_confirmed": "shopping", "extraction_id": "…" }`.
5. Create your own `Transaction` record and store the `extraction_id` on it (to link
   back and avoid re-importing).

**How the feedback loop works** (pure heuristics — no ML): each `feedback/category`
is logged; when the **same** merchant→category is confirmed **3×**, the API creates
a merchant rule and starts suggesting that category for that merchant in
`category_suggestion` going forward.

**What to store on your Transaction** (minimum): `email_extraction_id` (the `id` —
links back, dedupes against manual entries) and `extraction_confidence` (to show the
user how sure the import was). Use `GET /extractions/{id}/preview` to show the raw
email snippet on demand.

---

## 7. Category vocabulary & mapping

`category_suggestion` (when present) comes from this fixed vocabulary:
```
food_and_dining, groceries, transport, entertainment, shopping,
utilities, subscriptions, health, travel, atm_cash, transfers, other
```
Your app has its own categories, so map between them. Simplest for MVP: show your
own category list in the review UI with the API's suggestion **pre-selected** as the
best match; store `{api_suggestion → your_category_id}`. (A Phase-2
`POST /api/v1/config/categories` will let the API suggest *your* IDs directly.)

---

## 8. Account deletion (GDPR)

When a user deletes their account or disables the email feature:
```
DELETE /api/v1/users/{external_user_id}/data
X-API-Key: <key>
```
**200 response:**
```json
{ "external_user_id": "your-user-id", "aliases_deactivated": 1, "emails_scheduled_for_deletion": 142 }
```
This deactivates the user's aliases immediately and schedules their stored raw
emails for deletion after a grace period. Idempotent.

---

## 9. Data shapes

**Extraction detail** (`GET /api/v1/extractions/{id}`) — fictional example:
```json
{
  "id": "11111111-1111-1111-1111-111111111111",
  "email_id": "22222222-2222-2222-2222-222222222222",
  "external_user_id": "user-1234",
  "amount": "1250.00",              // STRING; null if not found
  "currency": "JMD",                // null if unknown
  "merchant_normalized": "Corner Cafe Kingston",
  "merchant_raw": "CORNER CAFE KINGSTON",
  "category_suggestion": null,      // string (from §7 vocab) or null
  "category_confirmed": null,       // set once the user confirms
  "transaction_date": "2026-05-01", // ISO date, or null
  "card_last4": "4821",             // or null
  "extraction_confidence": "0.921", // STRING 0–1
  "confidence_band": "high",        // "high" | "low_confidence"
  "duplicate_confidence": "0",      // "1" ⇒ exact-match exists, show a badge
  "status": "pending_review",
  "direction": "debit",             // "debit" | "credit"
  "is_probable_refund": false,      // true ⇒ a refund/reversal (a credit)
  "is_declined": false,             // true ⇒ declined charge — show, don't book
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
- **`direction`** — `"debit"` (a charge) or `"credit"` (a refund/reversal/deposit).
  `amount` stays a positive magnitude; `direction` carries the sign, so a refund
  **reduces** spending. Default is `debit` — a credit needs explicit refund/reversal
  language or a `Type` field (a bare "credit card" never counts).
  **`is_probable_refund: true`** marks a credit specifically detected as a refund.
- **`is_declined: true`** — a declined charge (e.g. `Status: DECLINED`). Surface it
  ("this charge was declined") but **don't book it**.

---

## 10. Webhook payload + signature

Every delivery is compact JSON `{ event, event_id, created_at, data }`:
```json
{
  "event": "extraction.created",
  "event_id": "outbox-row-uuid",
  "created_at": "2026-05-01T14:03:00+00:00",
  "data": { /* same fields as the extraction detail above */ }
}
```
**Events & `data` shapes:**

- **`extraction.created`** / **`extraction.failed`** — `data` is **exactly** the §9
  extraction-detail object, byte-for-byte identical to `GET /extractions/{id}`
  (so: `id`, `merchant_normalized`/`merchant_raw`, `card_last4`, … — **not**
  `extraction_id`/`merchant`). The failed one has money/date `null`.
- **`alias.first_email_received`** — fires **once**, the first time an email is
  accepted for an alias (the onboarding "forwarding works!" signal):
  ```json
  { "alias_hash": "ab12cd34ef56gh78", "external_user_id": "user-1234", "email_id": "…" }
  ```
- **`forwarding.verification`** — Gmail's "confirm your forwarding address" email,
  captured at the alias so the user can finish setup in-app:
  ```json
  { "alias_hash": "ab12cd34ef56gh78", "provider": "gmail",
    "code": null, "confirmation_url": "https://mail-settings.google.com/mail/…",
    "received_at": "2026-05-01T14:03:00+00:00" }
  ```
  **Gmail onboarding notes:** (1) current Gmail emails carry only the
  **`confirmation_url`** — `code` is usually `null`, so surface the URL for the user
  to **click** (we deliberately never click it server-side — that would let anyone
  wire their inbox to a victim's alias). (2) This event is **webhook-only** — there
  is no pull endpoint for it, so a configured webhook is **required** to complete
  Gmail forwarding setup. (3) Only Gmail is detected; Outlook forwarding generally
  needs no confirmation. Need another provider? We add its sender address (one line).

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

## 11. Endpoint reference

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

## 12. Handling errors & edge cases

| Situation | What your app should do |
|---|---|
| `extraction.failed` (or `status: "extraction_failed"`) | "We received a receipt but couldn't read it — enter manually." Don't drop it silently. |
| `duplicate_confidence: "1"` | Show a "possible duplicate" badge; the row is still live, user decides. |
| `confidence_band: "low_confidence"` | Show a "low confidence" badge; keep fields editable. |
| `direction: "credit"` / `is_probable_refund: true` | Book as a credit/refund, not a charge. Recommended: always route to manual review — never auto-confirm a credit. |
| `is_declined: true` | Show an informational "charge declined" card; don't book it. |
| Webhook delivery fails | The API retries with backoff. As a fallback, poll `GET /extractions`. |
| Alias unknown/deactivated | The API drops the email at the edge; surface alias status in your settings UI. |
| Cold start (~50s first request) | Generous timeouts + retry. |
| `401` | Bad/missing `X-API-Key`. |

---

## 13. Future enhancements (Phase 2+)

- Send your category taxonomy so the API suggests *your* category IDs directly.
- Batch confirm; a merchant→category rules editor backed by the API.
- Extraction analytics dashboard; OCR for PDF/image receipts.

---

*This document is the integration contract. Changes to the API must be reflected
here first, then coordinated with the budgeting app.*
