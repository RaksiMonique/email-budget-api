# Forwarded Email Ingestion

> Reconciled 2026-07-26 to match [redesign-summary.md](../architecture/redesign-summary.md). The previous version described Postmark Inbound Parse + Clerk + Redis (all removed) and a naïve `^From:` regex. Worker/queue mechanics live in [cloudflare-email-setup.md](cloudflare-email-setup.md); phase-by-phase tasks in [PLAN.md](../../PLAN.md).

## Overview

Users route financial emails to a unique alias, `{token}@fintrack.raksimoni.com`. Ingestion is fully Cloudflare-native:

```
Email Routing (catch-all) → Email Worker (edge alias check → R2 → Queue)
  → Consumer Worker → POST /internal/email-received → FastAPI (synchronous pipeline)
```

### Auto-forward only (MVP constraint)

MVP supports **server-side auto-forwarding** (Gmail Settings→Forwarding or a filter; Outlook rule), not manual "Fwd".

- Auto-forwarding **preserves the original `From:` header and `DKIM-Signature`**, so the original sender is recoverable at the header level.
- Manual client-side forwards rewrite `From:` to the user's own address and wrap the original in the body — **best-effort only**; extraction may fail.
- The budgeting app must walk users through creating the auto-forward rule during onboarding.

---

## Alias provisioning

**When:** the budgeting app calls `POST /api/v1/aliases` on user onboarding.

**Format:** `{token}@fintrack.raksimoni.com` where `token = secrets.token_urlsafe(12)` (~72 bits — not enumerable), uniqueness-checked against the `aliases` table.

```python
def new_alias_token() -> str:
    return secrets.token_urlsafe(12)   # ~72 bits; retry on the rare collision
```

The `aliases` table (`alias_hash`, `external_user_id`, `label`, `is_active`, …) is the **single source of truth for routing** — Cloudflare uses one catch-all rule, with no per-alias Cloudflare config. See [cloudflare-email-setup.md](cloudflare-email-setup.md).

---

## Forward unwrapping / sender resolution

The `From:` on an auto-forwarded email is *usually* the original sender — but not always. Resolve identity in priority order:

1. **`DKIM-Signature` `d=` domain** — survives forwarding, hardest to spoof → **primary**.
2. **`From:` header domain** — preserved by server-side auto-forward → secondary.
3. **Body-embedded sender block** — fallback for providers that rewrite headers. Robust parsing, *not* a single `^From:` regex: handle Gmail `On <date>, <name> <addr> wrote:`, Apple Mail, and HTML quote wrappers.

```python
def resolve_sender(headers, body) -> ResolvedSender:
    if d := dkim_d_domain(headers):           # e.g. d=chase.com
        return ResolvedSender(d, source="dkim", confidence=0.97)
    if fromdom := header_from_domain(headers):
        return ResolvedSender(fromdom, source="header", confidence=0.85)
    if bodydom := body_embedded_sender(body):
        return ResolvedSender(bodydom, source="body", confidence=0.6)
    return ResolvedSender(None, source="none", confidence=0.0)
```

Normalize forwarded subjects before classification/extraction:
```python
subject = re.sub(r'^\s*(fwd?|re):\s*', '', subject, flags=re.I)  # repeat for stacked prefixes
```

Classification keys on the **resolved** domain, never the raw `From:`. SPF will legitimately fail on forwarded mail — log it, never gate on it. Implementation: `app/extraction/sender_resolver.py` (Phase 1).

---

## Ingestion flow (durability)

FastAPI's `/internal/email-received` runs the whole pipeline **synchronously** and returns `200` only after the result is committed. Because Cloudflare Queues retry on any non-200, a crash or redeploy mid-processing is simply re-delivered — idempotent via `message_id` + the stable `r2_key`. No `BackgroundTasks`, no reaper. (The old "return 200 immediately" pattern was a fossil of Postmark's 30s webhook timeout.)

---

## Anti-abuse

| Protection | Implementation |
|-----------|---------------|
| Unknown / inactive alias | Email Worker validates at the edge (`GET /internal/aliases/{hash}`, cached) and `setReject`s **before** any R2 write or enqueue |
| Alias enumeration | `token_urlsafe(12)` (~72 bits); silent reject (no 404) so existence isn't leaked |
| Rate limiting | Per-alias limit in FastAPI `/internal/email-received` |
| DMARC/SPF | Logged for monitoring; **not** a block (auto-forward breaks SPF by design) |
| Large emails | R2 put limit; Worker 128 MB memory cap |
| Dead letters | Failed messages land in `email-processing-dlq`, visible for retry (not dropped) |

---

## Testing

- **Local (no cloud):** run the pipeline against `.eml` fixtures — `python -m app.extraction.run_fixture <path.eml>` — see [PLAN.md](../../PLAN.md) Phase 1.
- **Webhook seam:** POST a fixture payload straight to `/internal/email-received` (see the `curl` example in [cloudflare-email-setup.md](cloudflare-email-setup.md)) — no real email needed.
- **End-to-end:** auto-forward a real bank alert; confirm the R2 object, the queue message, and the resulting `ExtractionResult`.

---

*Worker + queue + wrangler details: [cloudflare-email-setup.md](cloudflare-email-setup.md). Architecture rationale: [redesign-summary.md](../architecture/redesign-summary.md).*
