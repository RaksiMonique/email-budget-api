# Open Technical Decisions — P.

> ℹ️ **Predates the 2026-05-17 redesign.** Some items here (e.g. Nylas vs. direct APIs) are Phase 3 concerns; others were already settled by the redesign. Cross-check [redesign-summary.md](../architecture/redesign-summary.md) and [PLAN.md](../../PLAN.md).

These questions should be resolved before or during Phase 2 implementation. They are deferred intentionally — answering them requires data from Phase 1 production usage.

---

## Decision 1: Nylas vs. Direct Gmail/Outlook APIs

**Question:** At what point does it make sense to bypass Nylas and implement Google Gmail API and Microsoft Graph API directly?

**Current stance:** Use Nylas for MVP and Phase 2. Evaluate after 12 months.

**Factors to evaluate:**
- Nylas monthly cost at your user volume (pricing is connection-based)
- Cost of 2–4 weeks engineering to build direct integrations
- Nylas uptime record (any outages that impacted users?)
- Microsoft Graph API quirks that Nylas currently absorbs

**Trigger for re-evaluation:** Nylas cost > $500/month OR Nylas causes two or more user-visible outages.

---

## Decision 2: Claude Model Routing Strategy

**Question:** Should there be a more dynamic routing strategy than "Haiku for classification, Sonnet for extraction"?

**Options:**
1. Fixed routing by task (current plan)
2. Route by email complexity (short/simple → Haiku, long/HTML → Sonnet)
3. Route by email type (merchant receipt → Haiku, bank statement → Sonnet)
4. Route by confidence — run Haiku first, escalate to Sonnet if confidence < 0.8
5. Always use Haiku (cheaper, accept lower accuracy)

**Data needed:** Production accuracy comparison between Haiku and Sonnet on real financial emails. Run an A/B test in Phase 1.

**Current assumption:** Sonnet is worth the cost for extraction. Revisit after 10K extractions.

---

## Decision 3: Forwarding Address Format

**Question:** Should forwarding addresses be:
- A) `{8-char-hex}@fintrack.raksimoni.com` (current plan)
- B) `{username}@fintrack.raksimoni.com` (user-chosen, memorable)
- C) Multiple addresses per user (one per card, one per category)

**Tradeoffs:**
- A is simpler, opaque (can't be guessed), easy to regenerate
- B is more memorable but requires uniqueness management and can expose username
- C adds significant UX complexity

**Recommendation:** Ship A in MVP. Evaluate user feedback. C is Phase 4.

---

## Decision 4: Raw Email Retention Default

**Question:** Should the default retention period be 30 days, 90 days, or user-configurable from the start?

**Considerations:**
- Longer retention: better AI reprocessing if templates improve
- Shorter retention: better privacy posture, lower R2 costs
- User-configurable from day 1: more complexity to implement

**Current plan:** 90 days default, user-configurable in settings. Review after launch — if no users change it, simplify to fixed 90 days.

**Open question:** Is 90 days defensible under GDPR? Does your legal opinion support it? *Need legal review.*

---

## Decision 5: Attachment Processing

**Question:** Should PDF/image attachments be processed for transaction extraction?

**Scenarios:**
- User forwards an email with a PDF invoice attached
- Bank sends statement as PDF attachment (no text body)
- Merchant sends HTML receipt + PDF receipt

**Options:**
1. Ignore attachments entirely (MVP approach)
2. Extract text from PDFs (Apache Tika or pypdf2)
3. OCR images and PDFs (AWS Textract — more accurate, costs $1.50/1000 pages)
4. Send attachment content to Claude alongside email body

**Current plan:** Skip attachments in MVP. Flag emails where extraction failed and body is empty — these are likely attachment-only emails. Address in Phase 3.

---

## Decision 6: Transaction Deduplication Window

**Question:** What is the right date window for fuzzy duplicate detection?

**Current plan:** ±3 days

**Tension:**
- Too narrow: miss bank alert vs. merchant receipt (bank alerts can arrive 1–2 days after transaction)
- Too wide: false duplicate matches for recurring transactions at similar intervals

**Recommendation:** Start with ±3 days. Monitor false positive rate in production. User feedback on "I got two transactions but they're different" is the signal to tighten the window.

---

## Decision 7: Webhook Delivery for Budget App

**Question:** What is the contract for the outbound webhook to the budget app?

**Unresolved:**
- What JSON payload shape does the budget app expect?
- Does the budget app expect idempotency keys?
- What retry policy should we implement?
- Should we support multiple webhook endpoints per user?

**Action:** Define the webhook contract with the budget app team before Phase 3 delivery.

**Draft payload:**
```json
{
  "event": "transaction.approved",
  "id": "evt_{uuid}",
  "created_at": "2026-05-06T10:00:00Z",
  "data": {
    "transaction_id": "uuid",
    "merchant": "Amazon",
    "amount": "45.99",
    "currency": "USD",
    "transaction_date": "2026-05-06",
    "category": "shopping",
    "transaction_type": "debit",
    "payment_method": "Visa",
    "card_suffix": "1234",
    "source": "forwarded_email",
    "email_id": "uuid"
  }
}
```

---

## Decision 8: Classification Model vs. Rules

**Question:** Long-term, should classification be entirely rule-based, entirely AI, or a hybrid?

**Current plan:** Hybrid (rules first, AI fallback)

**Consideration:** After 6 months of production data, evaluate:
- What % of emails require AI classification (vs. rules catching them)?
- What is the AI classification accuracy vs. rules?
- Could a fine-tuned local model replace Claude Haiku for classification?

**Data to collect from day 1:** Log which method classified each email and whether the user ever flagged the classification as wrong.

---

## Decision 9: pgvector vs. pg_trgm for Duplicate Detection

**Question:** Should duplicate detection use pg_trgm (trigram string similarity) or pgvector (embedding-based vector similarity)?

**pg_trgm pros/cons:**
- No embedding generation cost
- Good for exact-ish merchant name matching
- Struggles with semantic variations ("Whole Foods" vs. "Whole Foods Market" vs. "WHOLEFDS")

**pgvector pros/cons:**
- Handles semantic similarity ("McDonald's" vs. "McD" vs. "Golden Arches")
- Requires embedding generation per transaction (cost + latency)
- Requires pgvector extension

**Current plan:** Start with pg_trgm (simpler, no extra cost). Evaluate pgvector in Phase 3 if duplicate false negative rate is unacceptable.

---

## Decision 10: Compliance Jurisdiction

**Question:** Which data protection regulations apply?

**Minimum:**
- GDPR (if serving EU users)
- CCPA (if serving California users)

**Open questions:**
- Are you handling "sensitive personal data" (financial data) under GDPR Article 9? *Financial data is not explicitly Article 9 — but email content could be.*
- Do you need a Data Processing Agreement (DPA) with Nylas, Postmark, and Anthropic?
- Do you need a cookie consent banner? (If using analytics)
- COPPA: minimum age requirement? (18+ recommended given financial data)

**Action:** Legal review required before public launch. Draft privacy policy and terms of service that specifically address email content processing.

---

*Decisions should be revisited quarterly and closed out as answers emerge from production data.*
