# Hosting and Deployment

> ⚠️ **SUPERSEDED (pre-redesign).** Both the platform and the topology here are obsolete: hosting is now **Render** (decided 2026-07-26, replacing Railway), and the Celery workers / Beat / Redis / Clerk / Nylas env vars are removed. MVP deploys one FastAPI service (Render Starter + paid Postgres) + two Cloudflare Workers via wrangler. Authoritative: [PLAN.md](../../PLAN.md) Phase 9.

## MVP: Railway

Railway is the recommended starting point. Zero DevOps overhead, instant PostgreSQL and Redis, deploy-on-push.

### Railway Services

```
email-budget-api (project)
  ├── api              — FastAPI application
  ├── worker-ingest    — Celery email_ingest + ai_extraction queues
  ├── worker-scan      — Celery inbox_scan queue
  ├── worker-maintenance — Celery maintenance queue
  ├── beat             — Celery Beat scheduler
  ├── postgresql       — Managed PostgreSQL 16
  └── redis            — Managed Redis 7
```

**Environment variables (Railway secrets):**
```
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
CLERK_SECRET_KEY=sk_...
CLERK_PUBLISHABLE_KEY=pk_...
NYLAS_CLIENT_ID=...
NYLAS_CLIENT_SECRET=...
NYLAS_WEBHOOK_SECRET=...
POSTMARK_INBOUND_TOKEN=...
ANTHROPIC_API_KEY=sk-ant-...
CLOUDFLARE_R2_ACCOUNT_ID=...
CLOUDFLARE_R2_ACCESS_KEY_ID=...
CLOUDFLARE_R2_SECRET_ACCESS_KEY=...
R2_BUCKET_NAME=email-budget-raw
SENTRY_DSN=https://...
```

**Railway Dockerfile:**
```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# API service start command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Worker start command:**
```
celery -A app.celery worker -Q email_ingest,ai_extraction -c 5 --loglevel=info
```

---

## Production: AWS ECS Fargate

Migrate to AWS when Railway becomes a bottleneck or costs exceed ~$300/month.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  AWS Account                                            │
│                                                          │
│  ┌─────────────────────────────────────────────────┐   │
│  │  VPC (private subnets for DB/Redis)             │   │
│  │                                                   │   │
│  │  ALB → ECS Fargate (API tasks, autoscale 2–8)   │   │
│  │       → ECS Fargate (Celery ingest, autoscale)  │   │
│  │       → ECS Fargate (Celery AI, autoscale)      │   │
│  │       → ECS Fargate (Celery scan, 2 tasks)      │   │
│  │       → ECS Fargate (Celery beat, 1 task)       │   │
│  │                                                   │   │
│  │  RDS PostgreSQL 16 (db.t4g.medium, multi-AZ)   │   │
│  │  ElastiCache Redis 7 (cache.t4g.small)          │   │
│  │                                                   │   │
│  │  Cloudflare R2 (external, S3-compatible)         │   │
│  └─────────────────────────────────────────────────┘   │
│                                                          │
│  Route 53 → api.emailbudget.io → ALB                   │
│  CloudWatch + Sentry → Alerts                          │
└─────────────────────────────────────────────────────────┘
```

### ECS Task Definitions

**API Task:**
```json
{
  "cpu": "512",
  "memory": "1024",
  "command": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"],
  "healthCheck": {
    "command": ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"],
    "interval": 30,
    "timeout": 5,
    "retries": 3
  }
}
```

**Celery AI Extraction Worker Task:**
```json
{
  "cpu": "256",
  "memory": "512",
  "command": ["celery", "-A", "app.celery", "worker", "-Q", "ai_extraction", "-c", "3"]
}
```

### Autoscaling

```
API: scale on ALBRequestCountPerTarget > 100 (add task every 5 min, remove after 15 min idle)
Celery AI: scale on SQS/Redis queue depth for ai_extraction > 200
Celery Ingest: scale on queue depth > 500
```

---

## Frontend: Vercel

Next.js frontend deploys to Vercel automatically on push to main.

```
vercel.json:
{
  "env": {
    "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY": "@clerk_key",
    "NEXT_PUBLIC_API_URL": "https://api.emailbudget.io",
    "CLERK_SECRET_KEY": "@clerk_secret"
  }
}
```

API routes in Next.js handle:
- Clerk auth token forwarding
- Backend-for-frontend requests that need server-side secrets

---

## CI/CD: GitHub Actions

```yaml
# .github/workflows/deploy.yml
name: Deploy

on:
  push:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres: { image: postgres:16 }
      redis: { image: redis:7 }
    steps:
      - uses: actions/checkout@v4
      - name: Run tests
        run: pytest tests/ -x

  deploy-api:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build and push to ECR
        run: |
          aws ecr get-login-password | docker login --username AWS --password-stdin $ECR_REGISTRY
          docker build -t email-budget-api .
          docker push $ECR_REGISTRY/email-budget-api:$GITHUB_SHA
      - name: Deploy to ECS
        run: |
          aws ecs update-service --cluster email-budget --service api \
            --force-new-deployment

  run-migrations:
    needs: deploy-api
    runs-on: ubuntu-latest
    steps:
      - name: Run Alembic migrations
        run: |
          alembic upgrade head
```

---

## Database Migrations

```
alembic revision --autogenerate -m "description"
alembic upgrade head          # deploy
alembic downgrade -1          # rollback one step
```

Migrations run in CI before service deployment. All migrations must be backwards-compatible (no column renames that break the old version before new version starts).

**Migration safety rules:**
- Never rename a column in a single migration — add new + backfill + delete old
- Never add NOT NULL without a default value on existing tables
- Add indexes CONCURRENTLY in production to avoid locking

---

## Domain and Email Setup

**API domain:** `api.emailbudget.io` → ALB or Railway

**Inbound email domain:** `fintrack.raksimoni.com`
- MX record: `inbound.postmarkapp.com` (Postmark handles routing)
- Postmark Inbound Server configured to POST to `https://api.emailbudget.io/webhooks/inbound-email`

**Frontend domain:** `app.emailbudget.io` → Vercel

---

## Monitoring Stack

**MVP:**
- Sentry (FastAPI + Celery SDK integration)
- Railway metrics (CPU, memory, request count)

**Production:**
- Sentry: error tracking, performance monitoring
- Grafana Cloud: free tier for metrics + logs
- Loki: log aggregation from Celery and FastAPI (structured JSON)
- Custom Prometheus metrics:
  - `email_pipeline_total{status}` — emails per status
  - `extraction_confidence_histogram` — distribution of confidence scores
  - `celery_queue_depth{queue}` — queue depths
  - `claude_api_latency_seconds` — AI extraction latency
  - `duplicate_detection_rate` — % of emails flagged as duplicate

**Alerts:**
- Celery queue depth > 1000 for > 10 minutes
- Extraction failure rate > 20% in 15-minute window
- PostgreSQL connection count > 80% of max
- Claude API error rate > 5% in 5-minute window
- Any 5xx response spike in API

---

*See [deployment/queue-job-design.md](../deployment/queue-job-design.md) for Celery configuration.*
*See [architecture/scaling-strategy.md](../architecture/scaling-strategy.md) for when to migrate tiers.*
