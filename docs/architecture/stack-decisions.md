# Stack Decisions — Recommendations, Reasoning, and Tradeoffs

> Updated 2026-05-17 to reflect architectural redesign. See [architecture/redesign-summary.md](redesign-summary.md) for full rationale.

## Revised Stack (Current)

| Layer | Choice | Notes |
|-------|--------|-------|
| Backend framework | **FastAPI** (Python 3.12) | Unchanged from v1 |
| Database | **PostgreSQL 16** | Unchanged |
| ORM / query layer | **SQLAlchemy 2.0** (async) + **Alembic** | Unchanged |
| Email routing | **Cloudflare Email Routing** | Replaces Postmark |
| Email worker | **Cloudflare Email Worker** (TypeScript) | New |
| Message queue | **Cloudflare Queues** | Replaces Celery + Redis |
| Async processing | **FastAPI BackgroundTasks** (asyncio) | Replaces Celery workers |
| Storage | **Cloudflare R2** | Unchanged |
| Authentication | **API key** (service-to-service) | Replaces Clerk |
| Extraction | **Regex templates + heuristics** | Replaces Claude API (MVP) |
| Inbox connection | **Not in scope** | Removes Nylas |
| Frontend | **Not in scope** (budgeting app owns UI) | Removes Next.js |
| Hosting | **Railway** (MVP) → AWS ECS (prod) | Unchanged |
| Monitoring | **Sentry** | Unchanged |
| Testing | **pytest** + **httpx** + **wrangler** (Workers) | Simplified |

---

## C. Architecture Reasoning and Tradeoffs

### Backend Framework: FastAPI (Unchanged)

FastAPI remains the right choice. The justification shifts slightly:

- Previously justified by AI ecosystem (Python for Claude API). Now justified by: MIME parsing (`email`, `mailparser`), regex extraction, async I/O, Pydantic validation of extracted financial data, and overall Python ecosystem maturity.
- The async model handles concurrent email processing cleanly via `asyncio.create_task` without needing a separate worker process.
- Pydantic v2 is ideal for defining strict extraction result schemas — wrong data gets caught before it reaches the database.

**vs. TypeScript (Hono / Fastify):** TypeScript workers are used at the Cloudflare layer (Email Worker, Queue Consumer). The FastAPI service is the processing core where Python's MIME parsing and regex libraries have no TypeScript equivalent of equal quality. Don't unify on TypeScript at the cost of losing `email.parser`, `mailparser`, `html2text`, and `dateparser`.

---

### Email Routing: Cloudflare Email Routing (Replaces Postmark)

**Why Cloudflare:**
- Already using Cloudflare R2 for storage — same account, same billing, same API surface.
- Cloudflare Email Routing is free (no per-message cost up to very high volume).
- Native integration with Cloudflare Email Workers — email arrives and a Worker runs synchronously, no polling.
- Catch-all routing rule means alias provisioning doesn't require a Cloudflare API call per user — just a DB record.
- Built-in MX record management; no DNS configuration beyond enabling the service.

**vs. Postmark Inbound:**
- Postmark charges per email received at scale (~$1.50/1000 beyond free tier).
- Postmark requires a per-user inbound server or shared server with routing logic.
- Postmark provides better MIME parsing (Postmark sends pre-parsed JSON); Cloudflare delivers raw MIME bytes. Tradeoff: we write Python MIME parsing code, but gain no vendor dependency and lower cost.

**vs. AWS SES:**
- SES is the right long-term choice if moving fully to AWS. For MVP with Cloudflare already in use, SES adds unnecessary multi-cloud complexity.
- Migration path: abstract email storage behind `EmailStorageService`; swap Cloudflare Worker for SES Lambda trigger in Phase 3 if needed.

---

### Message Queue: Cloudflare Queues (Replaces Celery + Redis)

**Why Cloudflare Queues:**
- The Email Worker already runs inside Cloudflare. Pushing to a Cloudflare Queue is a single async method call from the Worker — no external network hop.
- Eliminates Redis as an infrastructure dependency in MVP.
- Cloudflare handles retry logic (up to 5 retries, dead letter queue).
- Free tier: 1 million queue operations per month. At even 100K emails/month, well within free.

**Why not Celery + Redis:**
- Celery requires a Redis instance running alongside the API — additional hosting cost and operational complexity for MVP.
- The processing pipeline is simple enough for `asyncio.create_task` in MVP: fetch from R2 → parse → extract → store → webhook. No multi-step workflow state management needed.
- **When to bring Redis back:** If processing tasks regularly exceed 30 seconds, if you need priority queues with different worker pools, or if you need task state inspection. These are Phase 3 concerns.

**Consumer pattern:**
Cloudflare Queue Consumer Worker → HTTP POST to FastAPI `/internal/email-received` → FastAPI `BackgroundTasks`. The Worker ACKs once FastAPI returns 200.

---

### Authentication: API Key (Replaces Clerk)

**Why API key:**
- This is a service API. The budgeting app is the only client. There are no end users directly authenticating.
- Simple `X-API-Key` header is sufficient for service-to-service auth.
- Keys are stored hashed (bcrypt) in the database; rotation is a DB update.
- Per-key permissions can be added later (read-only keys, admin keys).

**vs. Clerk:**
- Clerk was solving user auth + social login + OAuth. None of that is needed here. Clerk's SDK, pricing, and webhook complexity add overhead for zero benefit.

**vs. JWT:**
- JWT makes sense if multiple services need to verify tokens without calling back to this API. With one client (the budgeting app), API key simplicity wins.

**Internal webhook security:**
- Cloudflare Queue Consumer Worker → FastAPI: `X-Internal-Secret` shared secret.
- Email API → Budgeting app: HMAC-SHA256 webhook signature with timestamp.

---

### Extraction: Regex Templates + Heuristics (Replaces Claude API in MVP)

**Why no AI in MVP:**
Financial data extraction has an asymmetric error cost: **a wrong transaction amount is worse than no transaction**. Users who see incorrect amounts will immediately distrust the system. AI extraction introduces hallucination risk that is hard to bound.

The correct accuracy strategy:
1. **Known senders (template-based):** Amazon, Chase, PayPal, Stripe, Apple, Google, Uber, Netflix, and ~90 other common senders. Templates achieve 98%+ accuracy on these senders, which represent ~75–85% of all financial emails for typical users.
2. **Unknown senders (general regex):** Standard patterns for amounts (`$\d+\.\d{2}`), dates, card suffixes. Handles another ~10–15% with lower but acceptable accuracy.
3. **Extraction failure:** If neither produces a confident result — **surface the failure to the user** rather than guessing. The budgeting app shows "We received an email but couldn't parse it — enter manually."

No wrong data is better than wrong data.

**AI strategy for Phase 2:**
- Add Claude as a fallback *only* for emails where both template and regex produce zero results.
- Gate on a strict confidence threshold — if AI confidence < 0.85, treat as failure.
- Never use AI to "improve" a partial result (the improvement might introduce errors).
- See [ai-processing/extraction-strategy.md](../ai-processing/extraction-strategy.md).

**vs. always-AI approach:**
- Always-AI costs ~$0.001–0.003 per email and introduces latency and hallucination risk.
- Always-regex is free, fast (< 10ms), deterministic, and auditable.
- For financial data, deterministic and auditable wins over flexible.

---

### Removed: Nylas

Nylas solved inbox OAuth for Gmail and Outlook. Since inbox connection is not in scope (forwarded email only), Nylas has no role. If inbox connection is added in Phase 3, the `InboxConnectionService` interface designed for this purpose can be implemented with Nylas or direct provider APIs at that time.

---

### Removed: Next.js Frontend

This API is a backend service. The budgeting app owns all user-facing UI, including the transaction review flow. Building a separate frontend here would duplicate the review UI and create a confusing user experience (two apps to manage spending).

---

### Async Processing: BackgroundTasks (Replaces Celery Workers for MVP)

FastAPI's `BackgroundTasks` runs tasks in the same process after the response is sent. For MVP, email processing completes in 1–5 seconds (R2 fetch + MIME parse + regex extraction). This is well within the capability of asyncio concurrency.

**Limitations and migration path:**
- `BackgroundTasks` doesn't persist if the process crashes mid-task. The Cloudflare Queue handles retries — if FastAPI crashes before ACKing, the Consumer Worker retries.
- If tasks exceed 30s or you need task inspection, add Celery + Redis in Phase 3.
- The `process_email` function is written as a plain async function — swapping from `background_tasks.add_task(process_email, ...)` to `celery_task.delay(...)` requires only the call site to change.

---

### Database: PostgreSQL (Unchanged)

PostgreSQL remains the right choice for the same reasons: JSONB for variable metadata, `pg_trgm` for fuzzy merchant matching in duplicate detection, full-text search for email subject/sender search, and ACID guarantees on financial records.

**One addition from redesign:** The `aliases` table is new. It's a simple lookup table: `alias_hash → external_user_id`. Indexed on `alias_hash` for O(1) Worker lookup.

---

## What the Stack Looks Like End-to-End

```
Email arrives → Cloudflare Email Worker (TypeScript)
                  ↓ store .eml
                Cloudflare R2
                  ↓ push message
                Cloudflare Queue
                  ↓ Consumer Worker POSTs
                FastAPI /internal/email-received (Python)
                  ↓ BackgroundTasks
                process_email() async function
                  ↓ fetch .eml from R2 via boto3/r2
                  ↓ parse MIME (Python email library)
                  ↓ classify (rule-based)
                  ↓ extract (templates + regex)
                  ↓ enrich (merchant normalization)
                  ↓ duplicate detect (PostgreSQL fingerprint + pg_trgm)
                  ↓ store ExtractionResult (PostgreSQL)
                  ↓ webhook to budgeting app (httpx)
                Budgeting App receives webhook
```

---

*See [architecture/redesign-summary.md](redesign-summary.md) for full redesign rationale.*
*See [ingestion/cloudflare-email-setup.md](../ingestion/cloudflare-email-setup.md) for Cloudflare setup details.*
