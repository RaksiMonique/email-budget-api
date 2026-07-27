# Email Budget API — Project Documentation

> **Architecture updated 2026-05-17.** See [architecture/redesign-summary.md](architecture/redesign-summary.md) for the full rationale behind changes.

## A. Product Overview

Email Budget API is a standalone backend service that automatically converts forwarded financial emails into reviewable transaction extraction results. It is a component of a larger budgeting application — the budgeting app is its only client.

Users forward receipts, bank alerts, and invoices to a unique Cloudflare email alias. The system stores the raw email, parses it, extracts transaction fields using templates and regex heuristics, checks for duplicates, and notifies the budgeting app via webhook. The budgeting app surfaces the extraction to the user for review and confirmation.

**This API has no frontend.** The budgeting app owns all user-facing UI.

### Core Flow

```
User forwards email → Cloudflare alias → Email Worker → R2 + Queue
→ FastAPI processes → classifies → extracts → notifies budgeting app
→ Budgeting app: user reviews → confirms → category feedback sent back
```

### Core Outcome

> **Forwarded email → Classified → Extracted → ExtractionResult → Budgeting App Review**

---

## Documentation Index

| Section | File | Description |
|---------|------|-------------|
| **Redesign Summary** | [architecture/redesign-summary.md](architecture/redesign-summary.md) | Answers to 3 redesign questions, rationale for stack changes |
| **Architecture** | [architecture/overview.md](architecture/overview.md) | System diagram, component map, data flow |
| **Stack Decisions** | [architecture/stack-decisions.md](architecture/stack-decisions.md) | Revised stack with reasoning and tradeoffs |
| **System Modules** | [architecture/system-modules.md](architecture/system-modules.md) | Detailed module designs (some sections pre-redesign, superseded) |
| **Project Structure** | [architecture/project-structure.md](architecture/project-structure.md) | Folder layout, conventions, local dev setup |
| **Scaling** | [architecture/scaling-strategy.md](architecture/scaling-strategy.md) | Horizontal scaling, bottleneck analysis |
| **Core Workflows** | [workflows/core-workflows.md](workflows/core-workflows.md) | End-to-end data flows |
| **Cloudflare Setup** | [ingestion/cloudflare-email-setup.md](ingestion/cloudflare-email-setup.md) | Email Routing, Email Worker, Queue, Consumer Worker |
| **Forwarded Email** | [ingestion/forwarded-email.md](ingestion/forwarded-email.md) | Forwarding flow, alias lookup, abuse protection |
| **Inbox Connection** | [ingestion/inbox-connection.md](ingestion/inbox-connection.md) | (Phase 3 — deferred, Nylas OAuth) |
| **Extraction Strategy** | [ai-processing/extraction-strategy.md](ai-processing/extraction-strategy.md) | Heuristics-first: templates → regex → failure |
| **Confidence Scoring** | [ai-processing/confidence-scoring.md](ai-processing/confidence-scoring.md) | Per-field and overall confidence scores |
| **Rules Engine** | [parsing/rules-engine.md](parsing/rules-engine.md) | Merchant normalization, category suggestion |
| **Duplicate Detection** | [duplicate-detection/duplicate-detection.md](duplicate-detection/duplicate-detection.md) | Fingerprint + fuzzy duplicate detection |
| **API Skeleton** | [api/api-skeleton.md](api/api-skeleton.md) | All REST endpoints + outbound webhook events |
| **Webhook Strategy** | [api/webhook-strategy.md](api/webhook-strategy.md) | Inbound (Cloudflare) + outbound (budgeting app) webhooks |
| **Database Schema** | [database/entity-schema.md](database/entity-schema.md) | All entities and relationships |
| **Queue Design** | [deployment/queue-job-design.md](deployment/queue-job-design.md) | ⚠️ Pre-redesign (Celery/Redis) — superseded by PLAN.md Phases 2–3, 7 |
| **Auth Strategy** | [security/auth-strategy.md](security/auth-strategy.md) | ⚠️ Pre-redesign (Clerk/Nylas) — superseded; MVP uses API-key + internal secret (PLAN.md Phase 3) |
| **Privacy & Compliance** | [security/privacy-compliance.md](security/privacy-compliance.md) | GDPR, data retention, deletion |
| **Testing** | [testing/testing-strategy.md](testing/testing-strategy.md) | Test pyramid, extraction accuracy fixtures |
| **Hosting & Deployment** | [infrastructure/hosting-deployment.md](infrastructure/hosting-deployment.md) | Railway MVP, wrangler Workers deploy |
| **MVP Recommendation** | [mvp/mvp-recommendation.md](mvp/mvp-recommendation.md) | What to build, in what order |
| **Roadmap** | [future-roadmap/roadmap.md](future-roadmap/roadmap.md) | Phase-by-phase feature roadmap |
| **Open Decisions** | [future-roadmap/open-decisions.md](future-roadmap/open-decisions.md) | Unresolved technical questions |
| **Budgeting App Integration** | [integration/budgeting-app-integration.md](integration/budgeting-app-integration.md) | **Reference when building budgeting app integration** |

---

> **Note (2026-07-26):** docs tagged with a ⚠️/🔮 banner predate the 2026-05-17 redesign and are kept for history. Where they conflict, [PLAN.md](../PLAN.md) and [architecture/redesign-summary.md](architecture/redesign-summary.md) are authoritative.

## Quick Reference: Current Stack (Revised)

| Decision | Choice | Replaces |
|----------|--------|---------|
| Backend | FastAPI (Python) | — (unchanged) |
| Database | PostgreSQL 16 | — (unchanged) |
| Email routing | Cloudflare Email Routing | Postmark |
| Email worker | Cloudflare Email Worker (TS) | — (new) |
| Queue | Cloudflare Queues | Celery + Redis |
| Async processing | Synchronous handler + Cloudflare Queue retry (+ webhook outbox) | Celery / BackgroundTasks |
| Storage | Cloudflare R2 | — (unchanged) |
| Authentication | API key (service-to-service) | Clerk |
| Extraction | Regex templates + heuristics | Claude API |
| Inbox connection | Not in scope (Phase 3) | Nylas |
| Frontend | Not in scope (budgeting app owns) | Next.js |
| Hosting | Railway (MVP) | — (unchanged) |

---

## Key Design Principles (Post-Redesign)

1. **Wrong data is worse than no data.** If extraction isn't confident, mark as failed and tell the user — never guess.
2. **This API has no opinion about budgets.** It extracts facts from emails. Categories are suggestions. The budgeting app owns the final category.
3. **External_user_id, not user accounts.** This service identifies users by the ID the budgeting app provides. No user auth here.
4. **Cloudflare for transport, Python for processing.** Email Workers are thin shims. All intelligence is in FastAPI.
5. **Heuristics compound over time.** User category confirmations feed back into merchant rules. Accuracy improves without AI.

---

*Last updated: 2026-05-17*
*See [mvp/mvp-recommendation.md](mvp/mvp-recommendation.md) for build order.*
*See [integration/budgeting-app-integration.md](integration/budgeting-app-integration.md) for budgeting app contract.*
