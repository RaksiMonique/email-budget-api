# Project Folder Structure

> Reconciled 2026-07-26 to match [redesign-summary.md](redesign-summary.md). The previous version described the pre-redesign stack (Next.js frontend, Celery tasks, Nylas/Clerk/Postmark clients, Redis) and has been removed. See "What changed" at the bottom.

## Repository Layout

```
email-budget-api/
│
├── docs/                          ← Architecture and design documentation
│
├── backend/                       ← FastAPI application + extraction pipeline
│   ├── app/
│   │   ├── main.py                ← App factory, router registration, Sentry init, outbox poller startup
│   │   ├── config.py              ← Settings (pydantic-settings, env vars)
│   │   ├── dependencies.py        ← Shared FastAPI dependencies (get_db, require_api_key)
│   │   │
│   │   ├── api/
│   │   │   └── v1/                ← Thin route handlers
│   │   │       ├── aliases.py             ← POST/GET/DELETE aliases (+ internal GET for edge check)
│   │   │       ├── extractions.py         ← list / detail / preview / confirm / dismiss / reprocess
│   │   │       ├── feedback.py            ← POST /feedback/category
│   │   │       ├── config.py              ← webhook config + test
│   │   │       ├── users.py               ← privacy / data deletion / export
│   │   │       ├── stats.py               ← extraction stats
│   │   │       └── internal.py            ← POST /internal/email-received (synchronous pipeline)
│   │   │
│   │   ├── extraction/            ← Pipeline internals (pure, unit-testable, no cloud)
│   │   │   ├── mime_parser.py            ← raw .eml → text/html/headers/attachments
│   │   │   ├── sender_resolver.py        ← DKIM d= → From: → body fallback (forward-unwrapping)
│   │   │   ├── content_preparer.py       ← HTML→text, footer/quote stripping, 8k cap
│   │   │   ├── template_extractor.py     ← per-sender regex templates
│   │   │   ├── general_extractor.py      ← amount/date/card/currency regex (Decimal, never float)
│   │   │   ├── confidence_scorer.py      ← per-field + overall confidence, routing
│   │   │   └── run_fixture.py            ← local CLI harness: `python -m app.extraction.run_fixture <eml>`
│   │   │
│   │   ├── services/              ← Orchestration / business logic (no HTTP, no raw ORM)
│   │   │   ├── classification_service.py ← is_financial? email_type, confidence
│   │   │   ├── extraction_service.py     ← orchestrates the whole pipeline, persists results
│   │   │   ├── rules_engine.py           ← merchant normalization + category suggestion
│   │   │   ├── duplicate_service.py      ← fingerprint dedup (suppress exact only)
│   │   │   ├── webhook_delivery_service.py ← outbox poller + HMAC signing
│   │   │   └── privacy_service.py        ← deletion scheduling, retention
│   │   │
│   │   ├── models/                ← SQLAlchemy ORM models (see database/entity-schema.md)
│   │   ├── schemas/               ← Pydantic request/response schemas
│   │   │
│   │   ├── db/
│   │   │   ├── session.py         ← Async engine + session factory
│   │   │   ├── base.py            ← DeclarativeBase
│   │   │   └── repositories/      ← Data access objects
│   │   │
│   │   ├── integrations/
│   │   │   └── r2_client.py       ← Cloudflare R2 (boto3 / S3-compatible)
│   │   │
│   │   ├── security/
│   │   │   ├── api_key.py         ← X-API-Key validation (constant-time)
│   │   │   └── internal_secret.py ← X-Internal-Secret for /internal/* routes
│   │   │
│   │   └── seed/                  ← Seed data
│   │       ├── financial_senders.py ← financial_sender_registry
│   │       └── merchant_rules.py
│   │
│   ├── alembic/
│   │   ├── env.py                 ← configured for async
│   │   └── versions/
│   │
│   ├── tests/
│   │   ├── unit/                  ← extraction / classification / dedup / rules
│   │   ├── integration/           ← pipeline + API (httpx test client)
│   │   └── fixtures/
│   │       └── eml/               ← real auto-forwarded .eml corpus (gitignored — may hold PII)
│   │
│   ├── pyproject.toml             ← deps + ruff / mypy / pytest config
│   ├── Dockerfile
│   └── docker-compose.yml         ← local dev: PostgreSQL only (no Redis)
│
├── workers/                       ← Cloudflare Workers (TypeScript)
│   ├── email-ingest/              ← Email Worker
│   │   ├── src/index.ts           ← edge alias check → upload R2 → enqueue
│   │   └── wrangler.toml
│   └── email-queue-consumer/      ← Consumer Worker
│       ├── src/index.ts           ← Queue → POST /internal/email-received (ack on 200)
│       └── wrangler.toml
│
├── .github/
│   └── workflows/
│       ├── test.yml               ← pytest on every PR
│       └── deploy.yml             ← deploy on main push
│
├── .env.example                   ← All env vars, no values
├── .gitignore
├── PLAN.md                        ← MVP build plan (source of truth)
└── README.md
```

---

## Key Architectural Conventions

**Thin controllers, fat services:**
- API handlers validate input, call a service, return a response.
- Business logic lives in `services/`; data access goes through `db/repositories/` — no SQLAlchemy queries in the service layer.

**Extraction pipeline is pure and cloud-free:**
- Everything under `app/extraction/` runs against a `.eml` on disk with no network, R2, or DB — this is what Phase 1 builds and tests against `tests/fixtures/eml/`.
- `services/extraction_service.py` is the only layer that touches R2 and the DB; it wires the pure pieces together.

**No AI in MVP:** extraction is regex templates + heuristics. AI fallback is Phase 3.

**Async processing without Celery:** ingestion durability comes from Cloudflare Queues (at-least-once + retry); the FastAPI `/internal/email-received` handler processes synchronously and returns 200 only after commit. Outbound webhooks use a `webhook_outbox` table + poller. Celery/Redis return only if pipeline tasks outgrow this (Phase 3).

**Dependency injection via FastAPI:**
```python
# dependencies.py
async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session

@router.get("/extractions")
async def list_extractions(
    _: None = Depends(require_api_key),
    db: AsyncSession = Depends(get_db),
):
    ...
```

**No raw SQL in application code** — SQLAlchemy ORM. Exception: fuzzy matching (`pg_trgm`, Phase 2) uses `db.execute(text(...))` with bound parameters.

---

## Local Development Setup

```bash
# 1. Infrastructure (Postgres only)
docker-compose up -d

# 2. Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e .            # deps declared in pyproject.toml
alembic upgrade head
python -m app.seed          # seed financial senders + merchant rules
uvicorn app.main:app --reload --port 8000

# Phase 1 — run the extraction pipeline against a fixture (no cloud, no DB):
python -m app.extraction.run_fixture tests/fixtures/eml/chase_alert.eml

# 3. Cloudflare Workers (separate terminals)
cd workers/email-ingest        && npx wrangler dev
cd workers/email-queue-consumer && npx wrangler dev
```

### docker-compose.yml (local dev)
```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: email_budget_dev
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

volumes:
  postgres_data:
```

---

## What changed from the pre-redesign layout

Removed (see [redesign-summary.md](redesign-summary.md)):
- **`frontend/`** — no UI here; the budgeting app owns all user-facing screens.
- **`app/tasks/` (Celery)** — replaced by Cloudflare Queues + synchronous processing + webhook outbox.
- **`integrations/{nylas,clerk,postmark}_client.py`** and **`integrations/claude_client.py`** — vendors removed / AI deferred to Phase 3.
- **`api/webhooks/{postmark,nylas,clerk}.py`** — ingestion is now `POST /internal/email-received` from the Cloudflare Consumer Worker.
- **`security/jwt.py` (Clerk), `security/rate_limiter.py` (Redis)** — API-key auth + internal secret; rate limiting is in-process for MVP.
- **Redis** from docker-compose; **ngrok/Postmark** from the dev flow.

Added:
- **`workers/`** — the two Cloudflare Workers.
- **`app/extraction/sender_resolver.py`** and **`run_fixture.py`** — forward-unwrapping + the local test harness.
- **`tests/fixtures/eml/`** — real `.eml` corpus.

---

*This structure is the implementation blueprint. Each `services/` file maps to a module in [system-modules.md](system-modules.md) (partially pre-redesign); ingestion mechanics are in [../ingestion/cloudflare-email-setup.md](../ingestion/cloudflare-email-setup.md).*
