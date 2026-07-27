# Future Roadmap — O.

> ⚠️ **SUPERSEDED for MVP (pre-redesign).** The "Phase 1 — MVP" list here still names Postmark and Clerk. Authoritative MVP scope and phasing is [PLAN.md](../../PLAN.md); its post-MVP phases supersede this file.

## Phase 1 — MVP (Weeks 1–5)
*Goal: Prove the extraction pipeline with forwarded emails*

- Postmark inbound email forwarding
- Email classification (rule + AI)
- Transaction extraction (template + rules + Claude)
- Exact duplicate detection
- Pending transaction review API
- Basic privacy controls (delete email, delete account)
- Clerk auth + user provisioning
- Railway deployment
- Sentry error tracking

**Success metric:** 80%+ extraction success rate on forwarded financial emails.

---

## Phase 2 — Inbox Connection (Weeks 6–12)
*Goal: Add automatic inbox scanning*

- Gmail OAuth via Nylas
- Outlook OAuth via Nylas
- Scheduled inbox scanning (Celery Beat)
- Nylas webhook for real-time import
- Historical inbox scan (30-day lookback)
- Fuzzy duplicate detection (pg_trgm)
- Duplicate resolution UI
- Import History screen
- Import Jobs API
- Bulk approve/reject
- Merchant and category rules engine
- Rules management UI screens

**Success metric:** Users import 10+ transactions per week without manual forwarding.

---

## Phase 3 — Intelligence (Weeks 13–20)
*Goal: Make extraction smarter and reduce manual review*

- Auto-approve rules (user-configurable threshold)
- Budget app webhook outbox (reliable delivery)
- Multi-transaction email support (bank statements)
- Per-user extraction template learning (corrections feed back)
- Feedback loop: user corrections improve confidence calibration
- AI disambiguation for duplicate detection
- OCR pipeline for image-only receipts (AWS Textract)
- PDF attachment extraction
- GDPR data export (full JSON + CSV)
- Full audit log UI
- Grafana + Loki monitoring stack

**Success metric:** Auto-approve rate > 60% for returning users.

---

## Phase 4 — Enrichment and Rules (Weeks 21–28)
*Goal: Rich categorization and merchant intelligence*

- Community merchant rule library (high-confidence user rules promoted to system)
- External merchant database integration (Plaid Enrichment or similar)
- Merchant logo service
- Subcategory support (e.g., Food → Restaurants / Coffee / Fast Food)
- Recurring transaction detection and labeling
- Budget category mapping (map to user's budget app categories)
- International currency support improvements
- Smart suggestions ("You usually approve all Uber transactions, approve now?")

---

## Phase 5 — Platform (Months 7–12)
*Goal: Multi-user and enterprise features*

- Team/organization accounts (shared inbox connection for company expense tracking)
- Role-based access (admin, reviewer, viewer)
- Multiple forwarding addresses per user (one per card/account)
- Custom domain support (receipts@yourdomain.com)
- SSO/SAML for enterprise users
- Webhooks: subscribe to classification events, not just approved transactions
- Public API (API key auth for third-party budget app integrations)
- White-labeling / embedded widget option

---

## Future AI Enhancements

### Short Term (Phase 3–4)
- **Prompt caching**: System prompts cached in Claude API — reduce latency and cost by 80% on cached portion
- **Batch API**: Use Anthropic Batch API for non-urgent extractions (50% cost reduction)
- **Haiku for classification, Sonnet for extraction**: Already planned; fine-tune prompt per model
- **Structured outputs tuning**: Improve few-shot examples based on production extraction errors

### Medium Term (Phase 4–5)
- **Fine-tuned extraction model**: Train a small model on labeled extraction data (6 months of corrections)
  - Target: replace 80% of Claude extractions with local model
  - Deploy on GPU instance (AWS g4dn) for $0.0001/email vs $0.003 Claude Sonnet
- **Embedding-based merchant matching**: Sentence-transformers for semantic merchant name normalization
- **Anomaly detection**: Flag unusual transaction amounts (e.g., $9,999.99 when average is $50)

### Long Term (Phase 5+)
- **Local LLM option**: Ollama + Llama 3 for self-hosted / enterprise tier
  - Eliminates privacy concern of sending email to Anthropic
  - Required for enterprise GDPR / HIPAA compliance
- **Proactive suggestions**: "You haven't logged any grocery transactions this week — did you miss any receipts?"
- **Receipt matching to bank feed**: Match email receipts to bank transactions for reconciliation
- **Voice memo receipts**: User speaks "I spent $25 at the farmer's market" → creates transaction

---

*See [future-roadmap/open-decisions.md](open-decisions.md) for unresolved questions.*
*See [mvp/mvp-recommendation.md](../mvp/mvp-recommendation.md) for MVP scope.*
