# Queue / Background Job Design — H.

> ⚠️ **SUPERSEDED (pre-redesign).** Celery + Redis are removed. Async durability now comes from Cloudflare Queues (at-least-once retry) + synchronous processing in `/internal/email-received`, plus a `webhook_outbox` table for outbound delivery. See [PLAN.md](../../PLAN.md) Phases 2–3 & 7 and [cloudflare-email-setup.md](../ingestion/cloudflare-email-setup.md).

## Overview

The email processing pipeline is entirely asynchronous. The API layer receives events (webhooks, OAuth callbacks, manual triggers) and immediately enqueues work. Celery workers execute the pipeline steps independently, with retries and error isolation at each step.

## Technology: Celery + Redis

- **Broker:** Redis (also used as API cache)
- **Result backend:** Redis (for job status polling)
- **Beat scheduler:** Celery Beat (for scheduled inbox scans)
- **Workers:** Separate worker pools per queue

---

## Queue Architecture

### Queues

| Queue | Purpose | Workers | Concurrency |
|-------|---------|---------|-------------|
| `email_ingest` | Store + classify new emails | High volume | 10 per worker |
| `ai_extraction` | AI-based extraction calls | Rate-limited | 3 per worker |
| `deduplication` | Duplicate detection | Medium | 5 per worker |
| `inbox_scan` | Scheduled/manual inbox scans | Medium | 3 per worker |
| `webhooks` | Outbound webhook delivery | Medium | 5 per worker |
| `maintenance` | Retention, deletion, export | Low priority | 2 per worker |

### Rationale for Separation
- `ai_extraction` must be rate-limited separately to avoid Claude API rate limit exhaustion when a large inbox scan arrives
- `inbox_scan` can run slowly without blocking inbound webhook processing
- `maintenance` never competes with user-facing pipeline work

---

## Task Definitions

### email_ingest queue

#### `store_email(email_id: UUID)`
Stores raw email to R2 and creates ImportedEmail record.
- **Retry:** 3 times, 30s/120s/300s backoff
- **Idempotent:** checks if r2_key already set
- **On final failure:** mark status=storage_failed, Sentry alert
- **Chains to:** `classify_email`

#### `classify_email(email_id: UUID)`
Classifies email as financial/non-financial using rules + AI.
- **Retry:** 3 times
- **Idempotent:** checks if EmailClassification already exists
- **On final failure:** mark status=classification_failed, notify user
- **Chains to:** `extract_transaction` if financial, stop if not

### ai_extraction queue

#### `extract_transaction(email_id: UUID, extraction_index: int = 0)`
Extracts transaction fields using template/rules/AI.
- **Retry:** 3 times, 60s/180s/600s backoff (AI API may be temporarily down)
- **Idempotent:** checks if ExtractionResult already exists for (email_id, extraction_index)
- **Rate limiting:** Redis semaphore limits concurrent Claude API calls to 5 globally
- **On final failure:** mark status=extraction_failed, create FailedExtraction record
- **Chains to:** `detect_duplicates`

### deduplication queue

#### `detect_duplicates(extraction_result_id: UUID)`
Runs fingerprint + fuzzy duplicate detection.
- **Retry:** 3 times
- **Idempotent:** checks if duplicate check already ran
- **Chains to:** `create_pending_transaction` unless suppressed

#### `create_pending_transaction(extraction_result_id: UUID)`
Creates PendingTransaction record and fires outbound webhook.
- **Retry:** 3 times
- **Idempotent:** checks UNIQUE index on extraction_result_id

### inbox_scan queue

#### `scan_inbox(connection_id: UUID, lookback_days: int = 1, job_id: UUID)`
Polls Nylas API for new emails on a connection.
- **Retry:** 3 times (Nylas API outage)
- **Distributed lock:** Redis lock `scan_lock:{connection_id}` to prevent concurrent scans
- **Lock TTL:** 30 minutes (max scan duration)
- **Cancellable:** checks ImportJob.status before each batch
- **Progress updates:** writes emails_scanned to ImportJob record every 50 emails
- **Rate respect:** Nylas has 10 req/s limit; task uses asyncio.sleep between batches

#### `initial_inbox_scan(connection_id: UUID, lookback_days: int = 30)`
First-time full scan after OAuth connection. Lower priority.
- Same as `scan_inbox` but with longer lookback
- Runs in `inbox_scan` queue at low priority

### webhooks queue

#### `deliver_webhook(transaction_id: UUID, endpoint_url: str, payload: dict, secret: str)`
Delivers approved transaction to budget app webhook.
- **Retry:** 5 times, exponential backoff (1m/5m/15m/30m/60m)
- **Signature:** HMAC-SHA256 of payload using webhook secret
- **On all retries exhausted:** mark ApprovedTransaction.webhook_delivered=false, alert user

### maintenance queue

#### `purge_expired_emails(user_id: UUID)`
Deletes R2 objects + clears r2_key for emails older than retention_days.
- **Schedule:** daily at 2 AM UTC via Celery Beat
- **Batch size:** 100 emails per task invocation

#### `execute_account_deletion(user_id: UUID)`
Runs full data deletion cascade.
- **Triggered by:** POST /privacy/delete-account after grace period
- **Atomic-ish:** records deletion progress in a staging table; resumes if interrupted

#### `export_user_data(user_id: UUID, job_id: UUID)`
Collects and packages all user data for GDPR export.
- **Output:** JSON + CSV files uploaded to R2 with time-limited signed URL

---

## Celery Beat Schedule

```python
CELERYBEAT_SCHEDULE = {
    # Scan all active inbox connections every 15 minutes
    "scan-all-active-inboxes": {
        "task": "tasks.inbox_scan.scan_all_active_connections",
        "schedule": crontab(minute="*/15"),
        "options": {"queue": "inbox_scan"},
    },
    # Purge expired emails daily
    "purge-expired-emails": {
        "task": "tasks.maintenance.purge_all_expired_emails",
        "schedule": crontab(hour=2, minute=0),
        "options": {"queue": "maintenance"},
    },
    # Retry failed webhook deliveries
    "retry-failed-webhooks": {
        "task": "tasks.webhooks.retry_failed_deliveries",
        "schedule": crontab(minute="*/30"),
        "options": {"queue": "webhooks"},
    },
    # Check for stale import jobs (stuck > 1 hour)
    "cleanup-stale-jobs": {
        "task": "tasks.maintenance.cleanup_stale_jobs",
        "schedule": crontab(minute=0),
        "options": {"queue": "maintenance"},
    },
}
```

---

## Pipeline Chain

```python
# Celery chain for full email pipeline
from celery import chain

def enqueue_email_pipeline(email_id: str) -> None:
    chain(
        store_email.s(email_id).set(queue="email_ingest"),
        classify_email.s().set(queue="email_ingest"),
        extract_transaction.s().set(queue="ai_extraction"),
        detect_duplicates.s().set(queue="deduplication"),
        create_pending_transaction.s().set(queue="deduplication"),
    ).apply_async()
```

**Chain behavior:**
- If any step returns early (e.g., email is non-financial), remaining steps are skipped via result inspection
- Each step receives the `email_id` from the previous step's return value
- Failed steps write error state to PostgreSQL before raising; retries re-enter from that step

---

## Idempotency Pattern

Every task checks if its work is already done before executing:

```python
@celery_app.task(bind=True, max_retries=3)
def classify_email(self, email_id: str):
    # Idempotency check
    if db.query(EmailClassification).filter_by(email_id=email_id).exists():
        return email_id  # already classified, pass through chain

    try:
        result = run_classification(email_id)
        db.add(EmailClassification(...))
        db.commit()
        return email_id
    except ExternalAPIError as exc:
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))
```

---

## Redis Key Patterns

| Pattern | Purpose | TTL |
|---------|---------|-----|
| `scan_lock:{connection_id}` | Prevent concurrent inbox scans | 30 min |
| `claude_semaphore` | Rate limit Claude API calls | Rolling |
| `rate_limit:inbound:{address_hash}` | Per-forwarding-address rate limit | 1 hour sliding |
| `user_pending_count:{user_id}` | Cached pending transaction count | 5 min |
| `classify_cache:{sender_hash}` | Cache sender classification result | 24 hours |

---

## Error Handling Strategy

| Error Type | Action |
|-----------|--------|
| Transient (network, API 429) | Retry with exponential backoff |
| Permanent (invalid email format, AI refused) | Mark failed, store error, notify user |
| Storage failure | Compensating delete, retry |
| Rate limit exhausted | Queue task for delayed retry, don't drop |
| Worker crash | Celery's acks_late=True ensures re-queue on crash |

All worker errors are reported to Sentry with full context (email_id, user_id, task name, stack trace).

---

## Worker Configuration

```python
# Celery app configuration
app.conf.update(
    task_acks_late=True,                    # re-queue on worker crash
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,           # fair dispatch
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    result_expires=3600,                    # 1 hour result TTL
    task_soft_time_limit=300,               # 5 min soft limit
    task_time_limit=600,                    # 10 min hard limit
)
```

---

## Monitoring

- **Flower**: Celery monitoring UI — task rates, queue depths, worker status
- **Prometheus**: Custom metrics exported from Celery events
  - `celery_task_duration_seconds{task_name}`
  - `celery_queue_depth{queue_name}`
  - `celery_task_failures_total{task_name}`
- **Grafana alert:** Queue depth > 1000 in `ai_extraction` for > 10 minutes

---

*See [infrastructure/hosting-deployment.md](../infrastructure/hosting-deployment.md) for worker deployment configuration.*
