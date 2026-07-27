# Email Budget API

Backend service that turns **auto-forwarded financial emails** into structured transaction data for a budgeting app. Users auto-forward bank alerts / receipts to a unique alias; this service parses, classifies, extracts, dedupes, and webhooks the result to the budgeting app for user review.

- **Build plan / source of truth:** [PLAN.md](PLAN.md)
- **Current architecture:** [docs/architecture/redesign-summary.md](docs/architecture/redesign-summary.md)
- **Integration contract:** [docs/integration/budgeting-app-integration.md](docs/integration/budgeting-app-integration.md)

## Stack (post-redesign)

| Layer | Choice |
|-------|--------|
| Backend | FastAPI (Python), async SQLAlchemy + Alembic |
| Database | PostgreSQL 16 |
| Raw email storage | Cloudflare R2 |
| Email ingestion | Cloudflare Email Routing → Email Worker → Queue → Consumer Worker |
| Extraction | Regex templates + heuristics (no AI in MVP) |
| Auth | API key (service-to-service) + internal shared secret |
| Hosting | Railway |
| Monitoring | Sentry |

> This is a pure backend API. There is **no frontend** here — the budgeting app owns all user-facing UI.

## Layout

```
backend/          FastAPI app, extraction pipeline, tests + .eml corpus
workers/          Cloudflare Email Worker + Queue Consumer Worker
docs/             Architecture & design docs (redesign-summary.md is current)
PLAN.md           MVP build plan
```

See [docs/architecture/project-structure.md](docs/architecture/project-structure.md) for the full tree.
