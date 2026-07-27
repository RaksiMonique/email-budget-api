# Cloudflare Email Ingestion Setup

> ✅ **Current (Cloudflare-native)** — with two corrections applied inline 2026-07-26: (1) the Email Worker validates the alias **before** writing to R2 / enqueuing; (2) `/internal/email-received` processes **synchronously** and returns 200 only after commit (no `BackgroundTasks`). Aliases are `secrets.token_urlsafe(12)`, not the short `abc123` used in older examples. See [PLAN.md](../../PLAN.md) Phases 2–3.

## Overview

The email ingestion stack is fully Cloudflare-native:
- **Cloudflare Email Routing** — MX records, alias routing rules
- **Cloudflare Email Worker** — receives MIME email, stores to R2, publishes to Queue
- **Cloudflare Queues** — buffers email processing jobs
- **Cloudflare Queue Consumer Worker** — delivers jobs to FastAPI via HTTP

This replaces Postmark entirely.

---

## Cloudflare Services Used

| Service | Purpose | Cost |
|---------|---------|------|
| Email Routing | MX records + alias routing rules | Free |
| Email Workers | Receive and process inbound MIME | ~$0.50/million requests |
| R2 | Store raw .eml files | $0.015/GB/month, free egress |
| Queues | Buffer email processing jobs | Free up to 1M ops/month |
| Workers (consumer) | Deliver queue jobs to FastAPI | Included with Workers |

---

## Setup: Cloudflare Email Routing

### 1. DNS Configuration

In your Cloudflare dashboard, enable Email Routing. Here the inbound host is the **`fintrack.raksimoni.com`** subdomain of `raksimoni.com` — a subdomain keeps the existing apex `@raksimoni.com` mail routing untouched.

Cloudflare automatically adds the required MX records:
```
MX  route1.mx.cloudflare.net  priority 89
MX  route2.mx.cloudflare.net  priority 75
MX  route3.mx.cloudflare.net  priority 22
```

And a required SPF record:
```
TXT  "v=spf1 include:_spf.mx.cloudflare.net ~all"
```

### 2. Catch-All Rule → Email Worker

Set a **catch-all rule** on `fintrack.raksimoni.com` (a subdomain or the root domain) that routes all inbound email to your Email Worker:

```
Catch-all action: Send to Worker → email-ingest-worker
```

All aliases (abc123@fintrack.raksimoni.com, xyz789@fintrack.raksimoni.com) are handled by a single Worker.

### 3. Alias Routing Rules via API

When the budgeting app requests a new alias, this API calls the Cloudflare Email Routing API to register it:

```python
import httpx

async def create_cloudflare_alias(alias: str) -> None:
    """
    Creates a Cloudflare Email Routing rule for a new alias.
    The catch-all Worker already handles all @fintrack.raksimoni.com,
    so this is only needed if using address-specific rules.
    
    With a catch-all Worker, no per-alias Cloudflare rule is needed.
    The Worker looks up the alias in our database.
    """
    # With catch-all: nothing to do in Cloudflare.
    # We only store the alias in our own aliases table.
    pass
```

**Key insight:** Because the catch-all rule sends all `@fintrack.raksimoni.com` mail to the Worker, you do NOT need to create a Cloudflare routing rule for each user alias. The Worker receives all emails and performs alias lookup against your database (via an HTTP call to FastAPI or a Cloudflare KV lookup).

This simplifies alias management dramatically — Cloudflare only needs one rule (the catch-all), and your PostgreSQL `aliases` table is the source of truth.

---

## Cloudflare Email Worker

The Email Worker is a Cloudflare Worker that handles the `email` event trigger.

### Worker code (TypeScript)

```typescript
// workers/email-ingest/index.ts

export default {
  async email(message: EmailMessage, env: Env, ctx: ExecutionContext) {
    const toAddress = message.to;
    
    // Extract alias hash from "abc123@fintrack.raksimoni.com"
    const aliasHash = toAddress.split("@")[0].toLowerCase();
    
    // Reject clearly malformed addresses
    if (!aliasHash || aliasHash.length < 4) {
      message.setReject("Unknown address");
      return;
    }

    // Validate the alias AT THE EDGE — before any R2 write or enqueue.
    // Cache the result (Workers Cache/KV); drop unknown/inactive silently.
    const known = await isKnownAlias(aliasHash, env);  // GET /internal/aliases/{hash}
    if (!known) {
      message.setReject("Unknown address");
      return;
    }
    
    // Generate a unique email ID
    const emailId = crypto.randomUUID();
    
    // Read the raw email content (ReadableStream of MIME bytes)
    const rawBytes = await streamToArrayBuffer(message.raw);
    
    // Store raw .eml to R2
    const r2Key = `emails/${aliasHash}/${emailId}.eml`;
    await env.R2_BUCKET.put(r2Key, rawBytes, {
      httpMetadata: {
        contentType: "message/rfc822",
      },
      customMetadata: {
        alias: aliasHash,
        emailId: emailId,
        receivedAt: new Date().toISOString(),
      },
    });
    
    // Build queue message (lightweight — just references, no email body)
    const queueMessage = {
      email_id: emailId,
      alias_hash: aliasHash,
      r2_key: r2Key,
      from: message.from,
      to: message.to,
      // Extract subject from headers
      subject: message.headers.get("subject") ?? "",
      message_id: message.headers.get("message-id") ?? "",
      date_header: message.headers.get("date") ?? "",
      received_at: new Date().toISOString(),
    };
    
    // Push to Cloudflare Queue
    await env.EMAIL_QUEUE.send(queueMessage);
  },
};

async function streamToArrayBuffer(stream: ReadableStream): Promise<ArrayBuffer> {
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
  }
  const totalLength = chunks.reduce((sum, c) => sum + c.length, 0);
  const result = new Uint8Array(totalLength);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.length;
  }
  return result.buffer;
}

interface Env {
  R2_BUCKET: R2Bucket;
  EMAIL_QUEUE: Queue;
}
```

### wrangler.toml for Email Worker

```toml
name = "email-ingest-worker"
main = "src/index.ts"
compatibility_date = "2025-01-01"

[[email]]
type = "email"
name = "incoming"

[[r2_buckets]]
binding = "R2_BUCKET"
bucket_name = "email-budget-raw"

[[queues.producers]]
binding = "EMAIL_QUEUE"
queue = "email-processing"
```

---

## Cloudflare Queue Consumer Worker

A second Worker consumes from the queue and delivers to FastAPI.

```typescript
// workers/email-queue-consumer/index.ts

export default {
  async queue(batch: MessageBatch<EmailQueueMessage>, env: Env) {
    for (const message of batch.messages) {
      try {
        const response = await fetch(env.FASTAPI_INTERNAL_URL + "/internal/email-received", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Internal-Secret": env.INTERNAL_SECRET,
          },
          body: JSON.stringify(message.body),
          // Worker fetch timeout: 30 seconds
        });
        
        if (response.ok) {
          message.ack();
        } else {
          // FastAPI returned an error — let Cloudflare retry
          message.retry();
        }
      } catch (err) {
        // Network failure — let Cloudflare retry
        message.retry();
      }
    }
  },
};

interface EmailQueueMessage {
  email_id: string;
  alias_hash: string;
  r2_key: string;
  from: string;
  to: string;
  subject: string;
  message_id: string;
  date_header: string;
  received_at: string;
}

interface Env {
  FASTAPI_INTERNAL_URL: string;
  INTERNAL_SECRET: string;
}
```

### wrangler.toml for Consumer Worker

```toml
name = "email-queue-consumer"
main = "src/index.ts"
compatibility_date = "2025-01-01"

[[queues.consumers]]
queue = "email-processing"
max_batch_size = 10
max_batch_timeout = 5        # seconds to wait for a full batch
max_retries = 5
dead_letter_queue = "email-processing-dlq"
```

---

## FastAPI Internal Endpoint

```python
# app/api/internal/webhooks.py

@router.post("/internal/email-received")
async def email_received(
    payload: EmailReceivedPayload,
    request: Request,
):
    # Validate internal secret
    if not secrets.compare_digest(
        request.headers.get("X-Internal-Secret", ""),
        settings.INTERNAL_SECRET
    ):
        raise HTTPException(403)
    
    # Quick idempotency check (message_id dedup)
    if payload.message_id:
        existing = await email_repo.find_by_message_id(payload.message_id)
        if existing:
            return {"received": True, "duplicate": True}
    
    # Create ImportedEmail record immediately (fast, synchronous)
    email_id = await email_repo.create(
        alias_hash=payload.alias_hash,
        r2_key=payload.r2_key,
        from_address=payload.from_,
        subject=payload.subject,
        message_id=payload.message_id,
        received_at=payload.received_at,
        status="received",
    )
    
    # Run the pipeline SYNCHRONOUSLY; return 200 only after commit. No BackgroundTasks —
    # Cloudflare Queues retry on non-200, so a crash mid-processing is re-delivered
    # (idempotent via message_id + r2_key). Enqueue the outbound event in the same tx.
    await process_email(email_id)                         # parse → resolve → classify → extract → store
    await outbox_repo.enqueue_extraction_event(email_id)  # Phase 7 outbox
    
    return {"received": True, "email_id": str(email_id)}
```

**Why process synchronously:** the Consumer Worker's fetch timeout is generous and Cloudflare Queues are patient — far longer than the 1–5s a regex extraction needs. Returning 200 *only after* the result is committed means a crash or redeploy mid-processing isn't ACKed, so the queue re-delivers it — strictly more durable than the old `BackgroundTasks` fire-and-forget, and needs no reaper. See [PLAN.md](../../PLAN.md) Phase 3.

---

## Alias Management: API → Cloudflare

### Create alias flow

```python
# app/services/alias_service.py

async def create_alias(external_user_id: str, label: Optional[str]) -> Alias:
    # Generate unique hash
    alias_hash = secrets.token_urlsafe(12)  # ~72 bits (not the short "abc12345" of older drafts)
    full_address = f"{alias_hash}@fintrack.raksimoni.com"
    
    # Store in our database (source of truth for routing)
    alias = await alias_repo.create(
        alias_hash=alias_hash,
        full_address=full_address,
        external_user_id=external_user_id,
        label=label,
    )
    
    # No Cloudflare API call needed — catch-all Worker handles routing.
    # Worker looks up alias_hash in our DB via FastAPI call.
    
    return alias
```

### Worker alias lookup

The Email Worker can look up the alias owner with a fast call to FastAPI (or Cloudflare KV for sub-millisecond lookup):

```typescript
// Option A: HTTP lookup in Worker (adds ~50ms)
const lookupResponse = await fetch(
  `${env.FASTAPI_INTERNAL_URL}/internal/aliases/${aliasHash}`,
  { headers: { "X-Internal-Secret": env.INTERNAL_SECRET } }
);
if (!lookupResponse.ok) {
  // Unknown alias — silently reject (don't leak alias existence)
  message.setReject("Unknown address");
  return;
}

// Option B: Cloudflare KV (< 1ms, eventual consistency)
// Alias KV is populated when alias is created and invalidated when deleted
const aliasData = await env.ALIAS_KV.get(aliasHash);
if (!aliasData) {
  message.setReject("Unknown address");
  return;
}
```

**Recommendation for MVP:** Use Option A (HTTP lookup). Simpler, no KV sync required. Upgrade to Option B (KV) if Worker latency becomes an issue.

---

## Queue Design

### Queue: `email-processing`
- Messages: lightweight (< 1KB) — just IDs and metadata, not email body
- Max batch size: 10 messages per Consumer Worker invocation
- Retry: up to 5 times with exponential backoff (Cloudflare handles automatically)
- Dead letter queue: `email-processing-dlq` (failed after all retries)

### Dead Letter Queue Handling

A separate scheduled job (or Cloudflare Cron Trigger) monitors the DLQ:
```
GET /admin/failed-emails — lists emails in DLQ
POST /admin/failed-emails/{id}/retry — re-queues for processing
```

---

## Local Development

Cloudflare Workers don't run natively in local Python dev. For local development:

```bash
# Run wrangler dev for Email Worker (optional)
cd workers/email-ingest
wrangler dev

# OR: simulate the Cloudflare Worker webhook manually
# Use a test script that calls /internal/email-received directly

# test_ingest.sh
curl -X POST http://localhost:8000/internal/email-received \
  -H "X-Internal-Secret: dev_secret" \
  -H "Content-Type: application/json" \
  -d '{
    "email_id": "test-uuid-123",
    "alias_hash": "abc12345",
    "r2_key": "emails/abc12345/test-uuid-123.eml",
    "from": "receipts@amazon.com",
    "subject": "Your Amazon.com order",
    "message_id": "<test@amazon.com>",
    "received_at": "2026-05-17T10:00:00Z"
  }'
```

For local dev, raw email can be stored in a local directory instead of R2 (controlled by `STORAGE_BACKEND=local` env var).

---

## Anti-Abuse

| Protection | Implementation |
|-----------|---------------|
| Unknown alias | Worker rejects silently (`setReject`) — no 404 to avoid enumeration |
| DLQ monitoring | Failed emails visible in admin, not dropped |
| Large email protection | R2 put limit; Worker has 128MB memory limit |
| Spam | Cloudflare Email Routing has basic spam filtering; email DMARC/SPF headers logged |
| Rate limiting | Per-alias rate limit enforced in FastAPI `/internal/email-received` handler |

---

*See [architecture/redesign-summary.md](../architecture/redesign-summary.md) for why Cloudflare was chosen over Postmark.*
*See [workflows/core-workflows.md](../workflows/core-workflows.md) for the full pipeline flow.*
