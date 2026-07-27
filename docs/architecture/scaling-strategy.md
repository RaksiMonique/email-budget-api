# Scaling Strategy — N.

> ⚠️ **SUPERSEDED (pre-redesign).** The bottleneck analysis assumes Celery, Redis, and Nylas — all removed. Re-derive scaling against the current stack (Cloudflare Queues + FastAPI on Railway) when relevant. Authoritative stack: [redesign-summary.md](redesign-summary.md).

## Current Architecture Bottlenecks

At MVP scale (< 1,000 users), the architecture runs comfortably on a single Railway instance. The scaling strategy addresses what breaks first as usage grows.

---

## Scaling Tiers

### Tier 1: MVP (0–1K users, < 10K emails/month)
- Single FastAPI instance (Railway, 1 vCPU, 512MB RAM)
- Single Celery worker process
- Single PostgreSQL instance (Railway managed)
- Single Redis instance
- R2 handles any storage volume natively
- No horizontal scaling needed

**Cost estimate:** ~$50–100/month

---

### Tier 2: Growth (1K–10K users, 100K–1M emails/month)

**Bottleneck 1: **
- Claude API rate limits constrain thrAI extraction throughputoughput
- Solution: AI extraction queue with per-minute rate limiter
- Expand to Claude Haiku for more emails (faster + cheaper than Sonnet)
- Redis semaphore: max 10 concurrent Claude calls

**Bottleneck 2: Inbox scan frequency**
- 10K users × 4 scans/hour = 40K Nylas API calls/hour
- Solution: Dynamic scan intervals (active users more frequent, inactive users less)
- Separate Celery worker pool for inbox scanning

**Bottleneck 3: PostgreSQL query performance**
- Pending transaction list with fuzzy merchant search becomes slow
- Solution: Add pg_trgm indexes (required for Phase 2 duplicate detection)
- Add read replica for analytics queries
- Partition `imported_emails` and `audit_logs` by month

**Infrastructure:**
- API: 2–3 instances behind load balancer (Railway or AWS ECS)
- Celery workers: separate pools per queue
- PostgreSQL: RDS db.t4g.medium + read replica
- Redis: ElastiCache cache.t4g.small

**Cost estimate:** ~$300–600/month

---

### Tier 3: Scale (10K–100K users, 10M emails/month)

**Bottleneck 4: Email storage costs**
- 10M emails × avg 50KB = ~500GB/month
- Solution: Compress email content before R2 upload (zlib, ~60% reduction)
- Aggressive retention: default 30 days raw content
- Move cold content to R2 Intelligent Tiering

**Bottleneck 5: Duplicate detection query latency**
- Fuzzy search across 10M+ transactions per user becomes slow
- Solution: pgvector for embedding-based similarity search
- Pre-compute transaction embeddings asynchronously
- Limit fuzzy search to 90-day window per user

**Bottleneck 6: Nylas API costs**
- Nylas pricing scales with active connections
- Solution: Evaluate direct Gmail API + Microsoft Graph API implementation
- Abstract behind `InboxConnectionService` interface (designed for this)
- Build direct integrations, deprecate Nylas if cost > build cost

**Bottleneck 7: Celery worker coordination**
- High task volumes need better visibility and retry management
- Solution: Migrate complex pipelines to Temporal (workflow orchestration)
- Temporal handles task state, versioning, and replay natively

**Infrastructure:**
- API: ECS Fargate, 4–8 tasks, ALB
- Celery: ECS Fargate tasks per queue, autoscaling on queue depth
- PostgreSQL: RDS db.r6g.large + 2 read replicas
- Redis: ElastiCache cluster mode
- R2: Storage lifecycle rules

**Cost estimate:** ~$1,500–4,000/month

---

### Tier 4: Enterprise (100K+ users, 100M+ emails/month)

At this scale, consider:

**Database sharding:**
- Shard `imported_emails` and `pending_transactions` by `user_id` hash
- Or: move to CockroachDB (horizontally scalable, Postgres-compatible)

**Multi-region:**
- Deploy in EU and US regions for GDPR compliance (EU data stays in EU)
- R2 has multi-region support natively

**AI cost optimization:**
- Fine-tune a small local extraction model on labeled production data
- Run on GPU instances (AWS g4dn.xlarge)
- Route 80% of emails to local model, 20% complex emails to Claude
- Target: reduce Claude API spend by 70%

**Dedicated email infrastructure:**
- Own the MX records and SMTP infrastructure
- Use AWS SES for inbound email processing
- Eliminates Postmark dependency and per-email costs

---

## Per-Component Scaling Characteristics

| Component | Scales horizontally? | Bottleneck | Solution |
|-----------|---------------------|-----------|----------|
| FastAPI API | Yes — stateless | CPU/IO | Add instances |
| Celery workers | Yes — stateless | Claude API rate limit | Rate-limited queue |
| PostgreSQL | Read: yes, Write: hard | Write throughput | Connection pooling, PgBouncer |
| Redis | Yes — cluster mode | Memory | Increase instance size or cluster |
| R2 | Infinite | Cost | Compression, lifecycle rules |
| Nylas | Subscription tier | Cost | Self-build at scale |
| Claude API | Request rate limits | Throughput | Queue + rate limiter |

---

## PostgreSQL Connection Management

PostgreSQL has a connection limit (~100–300 depending on instance). FastAPI + Celery combined can easily exceed this.

**Solution: PgBouncer connection pooler**
```
FastAPI (10 workers × 10 connections) → PgBouncer → PostgreSQL (20 real connections)
Celery  (20 workers × 5 connections)  → PgBouncer ↗
```

Use transaction pooling mode (not session) — compatible with SQLAlchemy async.

---

## Queue Scaling

Celery workers autoscale on queue depth using CloudWatch/Railway metrics:

```
Scale up: ai_extraction queue depth > 500 for 5 minutes
Scale down: ai_extraction queue depth < 50 for 15 minutes
```

Separate worker pools prevent one busy queue from starving others.

---

## Caching Strategy

| Cache Key | Value | TTL |
|-----------|-------|-----|
| `classify_cache:{sender_domain_hash}` | Classification result | 24h |
| `user_pending_count:{user_id}` | Count of pending transactions | 5m |
| `merchant_rules:{user_id}` | Compiled merchant rules | 10m |
| `user:{clerk_id}` | User record | 5m |
| `connection_health:{connection_id}` | Connection status | 2m |

Redis cache invalidated on mutation via cache-aside pattern.

---

*See [infrastructure/hosting-deployment.md](../infrastructure/hosting-deployment.md) for deployment config.*
