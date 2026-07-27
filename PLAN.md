# Email Budget API — MVP Build Plan

> Architecture reference: [docs/architecture/redesign-summary.md](docs/architecture/redesign-summary.md)
> Integration contract: [docs/integration/budgeting-app-integration.md](docs/integration/budgeting-app-integration.md)

**Legend:**
- `[ ]` — Not started
- `[x]` — Complete
- `[~]` — In progress
- `[!]` — Blocked / needs external action
- `⛅` — External: Cloudflare dashboard or CLI action (not code)
- `🔧` — Code task

---

## Ingestion constraint (MVP): auto-forward only

Users configure a **server-side auto-forward rule** at their mail provider (e.g. Gmail → Settings → Forwarding, or a filter → "Forward it to") pointing at their alias. This is a deliberate product constraint:

- Server-side auto-forwarding **preserves the original `From:` header and `DKIM-Signature`**, so the original sender is recoverable at the header level.
- A manual client-side "Forward" rewrites `From:` to the user's own address and wraps the original inside the body — those are **best-effort only** and may fail extraction.
- The budgeting-app onboarding **must** walk users through creating the auto-forward rule (see Integration Tasks).

Sender identity is resolved in priority order: **DKIM `d=` domain → `From:` header domain → body-embedded sender block (fallback)**. SPF will legitimately fail on forwarded mail — log it, never gate on it.

---

## Changes folded in from external review (2026-07-14)

Traceability for this revision; the plan body below already reflects all of these.

1. **Corpus + local pipeline first.** A new Phase 1 builds the extraction pipeline as pure Python against a fixture folder of real `.eml` files, *before* any Cloudflare setup. Infra is commodity; extraction is where the project succeeds or fails.
2. **Forward-unwrapping / sender resolution is a real pipeline step** (DKIM `d=` → `From:` → body fallback), not an afterthought. Previously the only unwrapping logic lived in a stale Postmark-era doc and never reached the pipeline.
3. **Synchronous processing replaces `asyncio.create_task`.** The fire-and-forget pattern was a fossil of Postmark's 30s webhook timeout. Cloudflare Queues are patient and provide at-least-once retry, so the webhook processes to completion and returns 200 only after commit — durability for free, no reaper needed.
4. **Outbound webhook uses the outbox pattern in MVP** (pulled forward from Phase 3). `asyncio.sleep` retry loops die on redeploy; a `webhook_outbox` table + poller does not.
5. **Edge alias validation.** The Email Worker validates the alias (`GET /internal/aliases/{alias_hash}`, cached) *before* writing to R2 or enqueuing — closes the catch-all abuse hole.
6. **Alias token hardened** from 8-char hex (~32 bits) to `secrets.token_urlsafe(12)` (~72 bits).
7. **Dedup suppresses only exact matches** (`duplicate_confidence = 1.0`); anything less is flagged, never suppressed — matches the "0% false suppression" target.
8. **Confidence routing fixed.** Two routes only (`pending_review` vs `extraction_failed`); the 0.85 line is a confidence *badge*, not a second route. Auto-confirm stays OFF for MVP (a one-line policy flip later via `auto_approve_threshold`).
9. **Money is `Decimal` end-to-end.** `DECIMAL(15,4)` + `currency CHAR(3)` in the schema (already correct); enforce `Decimal` in code, never `float()`, and serialize amounts in webhooks as strings — never JSON floats.
10. **Doc reconciliation task** added to Phase 0 — ~20 docs still describe the pre-redesign stack (Postmark/Clerk/Nylas/Celery/Redis), which is how the plan drifted in the first place.

---

## Phase 0 — Prerequisites & Doc Reconciliation

These must be done before any code or Cloudflare setup.

- [x] ⛅ Inbound email hostname decided: **`fintrack.raksimoni.com`** (dedicated subdomain — isolates the service from personal `@raksimoni.com` mail; apex Email Routing stays untouched)
- [x] ⛅ Domain confirmed on Cloudflare (`raksimoni.com`; Email Routing already live on the apex — MX `route1/2/3.mx.cloudflare.net`, SPF present)
- [x] ⛅ Confirm R2 **and** Queues are available on the Cloudflare plan
  - [x] **Queues: confirmed** — both queues created 2026-07-27 via wrangler with no paid-plan block (account ID `f5ab158769e6cf5e226aaf83714e850f`)
  - [x] **R2: activated** 2026-07-27 (dashboard, payment method added)
- [x] ⛅ Create Cloudflare R2 bucket: `email-budget-raw`
  - [x] Bucket created 2026-07-27 (Standard storage class, via wrangler)
  - [x] Lifecycle rule `delete-raw-eml-after-90-days`: expire objects after 90 days (verified via `lifecycle list`)
  - [x] R2 API token created (scoped to bucket, Object Read & Write) → keys in `.env`; **verified 2026-07-27 with a PUT→GET→DELETE round-trip via boto3**
- [ ] ⛅ Create a Render account (or confirm existing) — hosting decided 2026-07-26: **Render**, replacing Railway. *Sole remaining Phase 0 item; not needed until Phase 9 — sign up at [render.com](https://render.com) with GitHub login whenever convenient.*
- [x] 🔧 Initialize git repository (branch `main`; `.gitignore`, `README.md`, `.env.example` added — not yet committed)
- [x] 🔧 Create project folder structure (scaffolded to CURRENT architecture: `backend/app/{extraction,services,…}`, `workers/{email-ingest,email-queue-consumer}`, `backend/tests/fixtures/eml/`)
- [x] 🔧 **Doc reconciliation:** current architecture is [docs/architecture/redesign-summary.md](docs/architecture/redesign-summary.md); ~20 docs still reference the removed stack (Postmark/Clerk/Nylas/Celery/Redis).
  - [x] Rewrote [docs/architecture/project-structure.md](docs/architecture/project-structure.md) to the current architecture
  - [x] Rewrote [docs/ingestion/forwarded-email.md](docs/ingestion/forwarded-email.md) (DKIM→From→body resolver, auto-forward only) and corrected [docs/ingestion/cloudflare-email-setup.md](docs/ingestion/cloudflare-email-setup.md) (edge alias check before R2; synchronous endpoint, no BackgroundTasks)
  - [x] Bannered the remaining pre-redesign docs (auth-strategy, webhook-strategy, core-workflows, queue-job-design, scaling-strategy, privacy-compliance, system-modules, hosting-deployment, entity-schema, inbox-connection, roadmap, open-decisions, rules-engine, testing-strategy); fixed inline refs in duplicate-detection + README
  - Rationale: the plan drifted because it was generated from docs describing the old architecture; reconcile once to stop it recurring

---

## Phase 1 — Extraction Corpus & Local Pipeline (pure Python, no cloud)

> Goal: a `.eml` file on disk goes → parsed → sender resolved → classified → extracted → scored, entirely locally, with tests. **This is the highest-risk part of the project; build and prove it before touching Cloudflare.**
> Docs: [docs/ai-processing/extraction-strategy.md](docs/ai-processing/extraction-strategy.md)
>
> **Status (2026-07-26):** the full pipeline is built and green on a synthetic Chase-alert fixture using **stdlib only** (`html2text`/`dateparser` are optional accuracy upgrades). Remaining: collect the real `.eml` corpus, add per-sender templates beyond `chase.com`, and grow the test suite from the corpus.

### Corpus collection (do this first)

- [~] 🔧 Create `backend/tests/fixtures/eml/` and collect **30–50 real auto-forwarded `.eml` files**: *(dir created; a synthetic fixture stands in for now — the real corpus is yours to gather)*
  - Each priority sender (banks first — highest signal), across Gmail and Outlook auto-forward
  - Include a few manual forwards (best-effort tier) and a few non-financial emails (newsletters) as negatives
  - Redact PII where needed but keep header structure (`From`, `DKIM-Signature`, `Subject`, body) intact
- [x] 🔧 Build a tiny local harness: `python -m app.extraction.run_fixture <path.eml>` prints resolved sender, classification, extracted fields, confidence
- [~] 🔧 Snapshot/assertion tests: each fixture has an expected extraction; regressions fail CI *(3 tests green on the synthetic fixture; grows with the corpus)*

### MIME parsing

- [x] 🔧 `app/extraction/mime_parser.py`:
  - Parse raw `.eml` bytes with Python `email` library
  - Extract `text_body`, `html_body`, `headers` (incl. `DKIM-Signature`), attachment list
  - HTML → text with `html2text` if no text body

### Sender resolution (forward-unwrapping)

- [x] 🔧 `app/extraction/sender_resolver.py`:
  - Parse `DKIM-Signature` header(s); extract `d=` domain → **primary** sender signal
  - Fall back to `From:` header domain (preserved by auto-forward)
  - Fall back to a body-embedded sender block for providers that rewrite headers (robust, not a single `^From:` regex — handle Gmail `On <date>, <name> <addr> wrote:`, Apple Mail, and HTML quote wrappers)
  - Normalize `Fwd:`/`Re:` subject prefixes: `re.sub(r'^\s*(Fwd?|Re):\s*', '', subject, flags=I)` (repeat for stacked prefixes)
  - Return `ResolvedSender(domain, source, confidence)` where `source ∈ {dkim, header, body}`

### Email classification

- [x] 🔧 `app/services/classification_service.py`: *(pure logic; DB persistence added in Phase 4)*
  - Load `financial_sender_registry` (known financial sender domains)
  - Check **resolved** sender domain against registry (not raw `From:`)
  - Check subject against `financial_subject_patterns` (regex list)
  - Assign `is_financial` + `email_type` + `confidence`; persist an `EmailClassification` (store the resolved sender + `source` for auditability)
  - If `is_financial=false` → mark `ImportedEmail.status = non_financial`, stop pipeline
- [~] 🔧 Seed `financial_sender_registry` for all 20 priority senders (below) *(seeded 11 incl. all P0 banks; finish alongside templates)*
- [x] 🔧 Seed `financial_subject_patterns`: receipt, invoice, payment, charged, purchase, order, transaction, statement, alert, refund, credit, debit, withdrawal, deposit, subscription, renewal, bill, confirmation

### Content preparation

- [x] 🔧 `app/extraction/content_preparer.py`:
  - HTML → plain text (`html2text`)
  - Strip email footers (unsubscribe blocks, legal boilerplate) and quoted-forward chrome
  - Whitespace normalization, 8000-char cap

### Sender template extraction

- [x] 🔧 `app/extraction/template_extractor.py`:
  - Load `ExtractionTemplate` records (cached in memory)
  - Match resolved sender domain → template
  - Run per-field regex against prepared content → `TemplateResult` (fields + confidence)
- [ ] 🔧 Write templates for **P0** senders (validate against corpus; banks first):
  - [x] chase.com *(provisional — validate against real corpus)*
  - [ ] bankofamerica.com
  - [ ] wellsfargo.com
  - [ ] amazon.com
  - [ ] paypal.com
  - [ ] stripe.com
  - [ ] apple.com
  - [ ] google.com (Google Play, Google One)
  - [ ] uber.com
- [ ] 🔧 Write templates for **P1** senders:
  - [ ] netflix.com
  - [ ] spotify.com
  - [ ] doordash.com
  - [ ] venmo.com
  - [ ] cashapp.com / cash.app
  - [ ] airbnb.com
  - [ ] lyft.com
  - [ ] instacart.com

### General regex extraction

- [x] 🔧 `app/extraction/general_extractor.py`:
  - Amount patterns (dollar, European, multi-currency) → **`Decimal`, never `float`**
  - Date patterns (ISO, US, EU, natural language) via `dateparser`
  - Card suffix patterns; currency code extraction
  - Return `GeneralResult` (fields + confidence)

### Merchant normalization + category suggestion

- [x] 🔧 `app/services/rules_engine.py`:
  - Load `merchant_rules` (cached, 10-min TTL, invalidate on mutation)
  - Apply in priority order: `starts_with` → `contains` → `exact` → `regex`
  - Generic cleanup: strip trailing transaction IDs, `#numbers`, title-case
  - Category suggestion: merchant → category lookup (seed with top merchants)

### Confidence scoring

- [x] 🔧 `app/extraction/confidence_scorer.py`:
  - Per-field confidence (template=0.97, regex=0.75, absent=0.0)
  - Overall: weighted average of required fields (amount 35%, date 20%, merchant 25%, currency 10%) + optional-field bonus
  - **Two routes only:** `< 0.60` or missing a required field → `extraction_failed`; otherwise → `pending_review`
  - The `0.85` line is a **confidence badge** on the `pending_review` record (`high` vs `low_confidence`), *not* a second destination
  - Auto-confirm is **OFF for MVP** (`aliases`/config `auto_approve_threshold` stays NULL). Enabling it later (e.g. auto-confirm ≥ threshold after N confirmed from a template) is a policy flip, not a schema change.

**Phase 1 complete when:** every fixture in `tests/fixtures/eml/` produces its expected extraction (or a deliberate `extraction_failed`) with `pytest` green — no Cloudflare, no Postgres required.

---

## Phase 2 — Cloudflare Infrastructure

> Goal: an auto-forwarded email lands in R2 and a queue message is produced — but only for **known aliases**.
> Docs: [docs/ingestion/cloudflare-email-setup.md](docs/ingestion/cloudflare-email-setup.md)

### Cloudflare Email Routing

- [ ] ⛅ Enable Cloudflare Email Routing on the domain
- [ ] ⛅ Confirm the required MX records were added (`dig MX fintrack.raksimoni.com` → Cloudflare MX)
- [ ] ⛅ Set SPF TXT if not auto-added: `"v=spf1 include:_spf.mx.cloudflare.net ~all"`
- [ ] ⛅ Create a catch-all routing rule → Email Worker `email-ingest-worker` (create placeholder Worker first)

### Cloudflare Queues

- [x] ⛅ Create queue: `email-processing` (done early, 2026-07-27 — id `9582781646144c2c882ca4e4314f44c7`)
- [x] ⛅ Create dead-letter queue: `email-processing-dlq` (id `83dfba16d45e4c7eaf017dd46529e69f`)
- [x] 🔧 Note queue IDs for `wrangler.toml` (recorded above)

### Email Worker (`workers/email-ingest`)

- [ ] 🔧 Create `workers/email-ingest/` (`wrangler.toml`, `src/index.ts`)
- [ ] 🔧 Implement Email Worker:
  - Receive `email` event; derive `alias_hash` from the recipient
  - **Validate alias at the edge FIRST:** `GET /internal/aliases/{alias_hash}` with `X-Internal-Secret`, result cached in the Workers Cache API / KV (short TTL). Unknown or inactive alias → **drop silently, do NOT write R2, do NOT enqueue.**
  - Only then: read raw MIME → `ArrayBuffer` → upload to R2 `emails/{alias_hash}/{email_id}.eml`
  - Push minimal message to `email-processing`:
    `{ email_id, alias_hash, r2_key, from, to, subject, message_id, date_header, received_at }`
- [ ] ⛅ Deploy Email Worker (`wrangler deploy`)
- [ ] ⛅ Bind R2 bucket + Queue (dashboard or `wrangler.toml`)
- [ ] 🔧 Manual test: auto-forward a real email → verify `.eml` appears in R2 **and** an unknown-alias test is dropped at the edge

### Queue Consumer Worker (`workers/email-queue-consumer`)

- [ ] 🔧 Create `workers/email-queue-consumer/` (`wrangler.toml`, `src/index.ts`)
- [ ] 🔧 Implement Consumer Worker:
  - Consume `email-processing` (batch size 10)
  - `POST /internal/email-received` to FastAPI with `X-Internal-Secret`
  - `message.ack()` on 200; `message.retry()` on non-200 (FastAPI processes synchronously, so 200 means fully stored — see Phase 3)
- [ ] ⛅ Set Worker env: `FASTAPI_INTERNAL_URL`, `INTERNAL_SECRET` (`openssl rand -hex 32`)
- [ ] ⛅ Deploy Consumer Worker; bind queue as consumer

**Phase 2 complete when:** auto-forwarding to a *known* alias produces an R2 object + a queue message, and forwarding to an *unknown* alias is dropped at the edge.

---

## Phase 3 — FastAPI Foundation

> Goal: the internal webhook validates, dedupes, and creates records — the seam the Phase 1 pipeline plugs into.
> Docs: [docs/architecture/stack-decisions.md](docs/architecture/stack-decisions.md)

### Project setup

- [ ] 🔧 Create `backend/`
- [ ] 🔧 `pyproject.toml` deps: `fastapi`, `uvicorn`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`, `pydantic[email]`, `boto3`, `httpx`, `python-multipart`, `sentry-sdk`, `html2text`, `dateparser`
- [ ] 🔧 `app/config.py` (pydantic-settings)
- [ ] 🔧 `app/main.py` (app factory, routers, Sentry init, outbox poller startup — see Phase 7)
- [ ] 🔧 `docker-compose.yml` for local PostgreSQL

### Database schema

- [ ] 🔧 `alembic init` + async `env.py`
- [ ] 🔧 SQLAlchemy models:
  - `aliases` — `alias_hash`, `external_user_id`, `label`, `is_active`, `emails_received`, `created_at`
  - `imported_emails` — `alias_hash`, `r2_key`, `from_address`, `resolved_sender_domain`, `sender_source`, `subject`, `message_id`, `received_at`, `status`, `processing_errors`
  - `email_classifications` — `email_id`, `is_financial`, `email_type`, `confidence`, `method`
  - `extraction_results` — extracted fields (`amount DECIMAL(15,4)`, `currency CHAR(3)`, …) + confidence + status + `external_user_id` + `fingerprint`
  - `extraction_snippets` — `extraction_id`, `raw_snippet`, `snippet_type`
  - `extraction_templates` — `sender_domain`, patterns, `success_count`, `failure_count`
  - `merchant_rules` — `pattern`, `match_type`, `normalized_name`, `category`, `confirmation_count`
  - `category_feedback_log` — `extraction_id`, `merchant_normalized`, `category_confirmed`, `created_at`
  - `duplicate_matches` — `extraction_id`, `candidate_id`, `candidate_type`, `duplicate_confidence`
  - `import_audit_logs` — `email_id`, `action`, `previous_status`, `new_status`, `timestamp`
  - `api_keys` — `key_hash`, `label`, `created_at`, `last_used_at`
  - `webhook_config` — `webhook_url`, `webhook_secret_encrypted`, `created_at`
  - **`webhook_outbox`** — `id`, `event_type`, `payload_json`, `target_url`, `status` (`pending`/`delivered`/`failed`), `attempts`, `next_attempt_at`, `last_error`, `created_at`, `delivered_at` (Phase 7)
- [ ] 🔧 `alembic revision --autogenerate -m "initial_schema"` → `alembic upgrade head`
- [ ] 🔧 Index `extraction_results.fingerprint` and `extraction_results.external_user_id`

### Authentication middleware

- [ ] 🔧 `app/security/api_key.py` — `X-API-Key`, constant-time compare against hashed keys, `Depends(require_api_key)`
- [ ] 🔧 `app/security/internal_secret.py` — `X-Internal-Secret` for `/internal/*`
- [ ] 🔧 Seed at least one API key (hashed)

### Alias management endpoints

- [ ] 🔧 `POST /api/v1/aliases` — create alias, generate **`secrets.token_urlsafe(12)`** (~72 bits) token, uniqueness-checked, store in DB
- [ ] 🔧 `GET /api/v1/aliases?external_user_id=` — list user's aliases
- [ ] 🔧 `DELETE /api/v1/aliases/{id}` — deactivate (`is_active=false`)
- [ ] 🔧 `GET /internal/aliases/{alias_hash}` — edge validation lookup for the Email Worker (fast, cacheable; returns active/inactive)

### Internal webhook endpoint (synchronous)

- [ ] 🔧 `POST /internal/email-received`:
  - Validate `X-Internal-Secret`
  - Validate alias exists & active (defense in depth; the Worker already checked at the edge)
  - `message_id` dedup — if already processed, return `200` immediately (idempotent)
  - Create `ImportedEmail` (status=`received`)
  - **Run the full pipeline synchronously** (Phase 4): parse → resolve sender → classify → extract → score → store
  - Write the outbound event to `webhook_outbox` in the *same transaction* (Phase 7)
  - Return `200` **only after commit**. On any exception → return `5xx` so Cloudflare Queue retries. Reprocessing is safe (keyed on `message_id`, stable `r2_key`).
  - ❌ **No `asyncio.create_task`** — that fire-and-forget pattern was a Postmark-30s-timeout fossil; Cloudflare Queues are patient and give retry for free.

### R2 client

- [ ] 🔧 `app/integrations/r2_client.py` — `get_object(r2_key)`, `delete_object(r2_key)`, configured from `R2_*` env

**Phase 3 complete when:** POSTing a fixture payload to `/internal/email-received` runs the real pipeline and leaves an `ExtractionResult` (or `extraction_failed`) row + a `webhook_outbox` row, all before the 200.

---

## Phase 4 — Service Integration

> Goal: wire the proven Phase 1 pipeline behind the Phase 3 webhook, reading real bytes from R2.

- [ ] 🔧 `app/services/extraction_service.py` — orchestrates: fetch `.eml` from R2 → `mime_parser` → `sender_resolver` → `classification_service` → `content_preparer` → `template_extractor` → `general_extractor` → merge → `rules_engine` → `confidence_scorer` → persist
  - Creates `ExtractionResult` + `ExtractionSnippet`
  - Updates `ImportedEmail.status` and template `success_count`/`failure_count`
  - Enqueues `extraction.created` / `extraction.failed` to `webhook_outbox`
- [ ] 🔧 Confirm `Decimal` flows end-to-end (no `float()` between extractor and DB)

**Phase 4 complete when:** auto-forwarding a real bank alert end-to-end (Cloudflare → R2 → queue → FastAPI) produces a populated `ExtractionResult` with `email_type=bank_alert`.

---

## Phase 5 — Duplicate Detection

> Docs: [docs/duplicate-detection/duplicate-detection.md](docs/duplicate-detection/duplicate-detection.md)

- [ ] 🔧 `app/services/duplicate_service.py`:
  - Email-level dedup: `message_id` uniqueness per `external_user_id` (already enforced in the webhook)
  - Transaction-level dedup:
    - Fingerprint: `SHA-256(normalized_amount + normalized_merchant + transaction_date)` (amount normalized from `Decimal` → minor-unit integer string so JMD/USD never collide)
    - Query `extraction_results` for the fingerprint, **scoped to `external_user_id`**
    - **Suppress only on exact match** (`duplicate_confidence = 1.0` → status=`duplicate_suppressed`)
    - Anything short of an exact fingerprint match → create a `DuplicateMatch` and **flag** it (status stays `pending_review`); never suppress `< 1.0` in MVP
    - Fuzzy matching (`pg_trgm`, the 0.60–0.99 band) is Phase 2 — keep the suppress-only-exact rule so that band never silently drops a unique transaction

---

## Phase 6 — Extraction Results API

> Docs: [docs/api/api-skeleton.md](docs/api/api-skeleton.md)

- [ ] 🔧 `GET /api/v1/extractions` — paginated; filter by `external_user_id`, `status`, date range
- [ ] 🔧 `GET /api/v1/extractions/{id}` — full detail incl. `field_confidences`, `duplicate_matches` (amounts as decimal strings)
- [ ] 🔧 `GET /api/v1/extractions/{id}/preview` — fields + raw snippet for the review UI
- [ ] 🔧 `POST /api/v1/extractions/{id}/confirm` — idempotent
- [ ] 🔧 `POST /api/v1/extractions/{id}/dismiss` — with reason
- [ ] 🔧 `POST /api/v1/extractions/{id}/reprocess` — re-fetch from R2, re-run pipeline
- [ ] 🔧 `POST /api/v1/feedback/category` — confirmed category → after 3+ same-merchant/same-category confirmations, create/update a `merchant_rules` entry
- [ ] 🔧 `GET /api/v1/stats/extraction?external_user_id=` — success rate, top senders, top failures

---

## Phase 7 — Outbound Webhook (Outbox Pattern)

> Docs: [docs/api/webhook-strategy.md](docs/api/webhook-strategy.md)

- [ ] 🔧 `POST /api/v1/config/webhook` — store webhook URL + encrypted secret
- [ ] 🔧 `POST /api/v1/config/webhook/test` — send a test payload, return delivery result
- [ ] 🔧 `app/services/webhook_delivery_service.py`:
  - Events are enqueued to `webhook_outbox` transactionally during extraction (Phase 4) — never delivered inline in the request (so queue-ack never depends on the budgeting app being up)
  - A poller (FastAPI startup background loop **or** Cloudflare Cron) claims due rows (`status=pending AND next_attempt_at <= now`), signs HMAC-SHA256 + timestamp, `POST`s to `target_url`
  - On 2xx → `status=delivered`, stamp `delivered_at`. On failure → increment `attempts`, set `next_attempt_at` by backoff **immediate → 1m → 5m → 15m → 1h**, then `status=failed` (surfaced for inspection)
  - Replaces the `asyncio.sleep` retry loop, which does not survive redeploys
- [ ] 🔧 Enqueue `extraction.created` after successful extraction; `extraction.failed` after failure

---

## Phase 8 — Privacy and Data Deletion

> Docs: [docs/security/privacy-compliance.md](docs/security/privacy-compliance.md)

- [ ] 🔧 `DELETE /api/v1/users/{external_user_id}/data`:
  - Deactivate all aliases for this user
  - Schedule all `ImportedEmail` R2 objects for deletion (`pending_deletion_at = NOW() + 30 days`)
  - Return count scheduled
- [ ] 🔧 `GET /api/v1/users/{external_user_id}/data-export` — stub (Phase 2 full impl)
- [ ] ⛅ R2 lifecycle rule to delete objects past `pending_deletion_at`, OR a nightly Cron Worker that lists + deletes expired objects
- [ ] 🔧 Retention job (Cron Worker or scheduled FastAPI task): delete R2 objects where `received_at < NOW() - retention_days AND r2_key IS NOT NULL`, then null `r2_key`

---

## Phase 9 — Deployment and Hardening

### Render deployment

- [ ] 🔧 `backend/Dockerfile`
- [ ] 🔧 Optional: `render.yaml` Blueprint (web service + Postgres as code — keeps infra reviewable in git)
- [ ] ⛅ Create Render **Web Service** from the GitHub repo (runtime: Docker, root dir `backend/`)
  - ⚠️ **Starter ($7/mo) or higher** — free instances spin down after ~15 min idle and cold-start in ~30–60s, which breaks the <10s processing target and delays every first email after a quiet period
  - Set a health check path (e.g. `/healthz` — add the endpoint in Phase 3's `main.py`)
- [ ] ⛅ Create **Render PostgreSQL** instance
  - ⚠️ Free Postgres **expires after 30 days** (14-day grace, then deleted) — use a paid tier for anything holding real data
  - Render issues `DATABASE_URL` as `postgres://…` — `config.py` must rewrite the scheme to `postgresql+asyncpg://`
  - Use the **internal** connection URL (same-region private network) for the app
- [ ] ⛅ Set env vars: `DATABASE_URL`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `INTERNAL_SECRET`, `SENTRY_DSN`, `ENVIRONMENT=production`
- [ ] 🔧 `alembic upgrade head` as Render **Pre-Deploy Command** (paid-instance feature — Starter covers it)
- [ ] ⛅ Auto-deploy on push to `main` (Render default when connected to GitHub)
- [ ] ⛅ Update Consumer Worker `FASTAPI_INTERNAL_URL` → `https://<service>.onrender.com`; redeploy

### Cloudflare Workers final deploy

- [ ] ⛅ Deploy Email Worker + Consumer Worker to production (`wrangler deploy --env production`)
- [ ] ⛅ Confirm catch-all rule points to the production Email Worker

### Sentry

- [ ] ⛅ Create Sentry project (Python/FastAPI); add SDK init to `app/main.py`
- [ ] ⛅ Sentry on Workers optional — `wrangler tail` for logs in MVP

### Security checklist

- [ ] 🔧 `X-Internal-Secret` ≥ 32 random bytes, not in code
- [ ] 🔧 R2 bucket private (no public access)
- [ ] 🔧 API keys stored hashed (bcrypt or SHA-256)
- [ ] 🔧 Webhook secret encrypted at rest
- [ ] 🔧 Alias tokens ≥ 72 bits (`secrets.token_urlsafe(12)`)
- [ ] 🔧 All SQL parameterized (SQLAlchemy ORM — no raw f-strings)
- [ ] 🔧 Rate limiting on `/internal/email-received`: max 10 concurrent per alias
- [ ] 🔧 `secrets.compare_digest` for all HMAC/token comparisons
- [ ] 🔧 Amounts serialized as decimal strings (never JSON floats) across all API + webhook payloads

---

## Phase 10 — End-to-End Validation

> Use **auto-forwarded** fixtures. Manual forwards are a best-effort tier and may fail — validate them separately, don't gate MVP on them.

- [ ] Auto-forward a Chase bank alert → `email_type=bank_alert`, amount/merchant/date populated
- [ ] Auto-forward an Amazon receipt → `ExtractionResult` with amount, merchant, date
- [ ] Auto-forward a non-financial email (newsletter) → `status=non_financial`, no extraction
- [ ] Auto-forward the same email twice → second yields `duplicate_confidence=1.0`, `duplicate_suppressed`
- [ ] Forward to an **unknown/inactive alias** → dropped at the edge (no R2 object, no queue message)
- [ ] Kill FastAPI mid-batch, restart → queue re-delivers, no email stuck in `received`
- [ ] `POST /extractions/{id}/confirm` → `status=confirmed` (idempotent on repeat)
- [ ] `POST /feedback/category` ×3 same merchant/category → `merchant_rules` entry created
- [ ] `DELETE /users/{id}/data` → aliases deactivated, R2 deletion scheduled
- [ ] Webhook: budgeting app receives `extraction.created` with correct payload and verifies the HMAC signature
- [ ] Redeploy with pending outbox rows → delivery resumes (no lost webhooks)

**Phase 10 complete = MVP is live.**

---

## MVP Success Criteria

| Metric | Target |
|--------|--------|
| Extraction success rate on top-20 senders (banks + merchants) | ≥ 95% |
| End-to-end processing time (email received → webhook enqueued) | < 10 seconds |
| False positive rate (non-financial → financial) | < 2% |
| Duplicate false suppression rate (unique tx suppressed) | 0% — suppress exact fingerprint matches only |
| Webhook delivery within 1 hour (outbox-backed) | > 99% |

> Note: banks + merchants are in MVP scope. Bank alerts are short and highly templated; merchant receipts (line items, tax, tips, split shipments) are harder — expect them to consume most of the accuracy budget on the ≥95% target.

---

## Budgeting App Integration Tasks
> Tasks in the budgeting app codebase, not this one.
> Full contract: [docs/integration/budgeting-app-integration.md](docs/integration/budgeting-app-integration.md)

- [ ] Store Email API key securely in budgeting app config
- [ ] On user onboarding (email feature enabled): call `POST /aliases` → store returned alias
- [ ] **Auto-forward setup UX:** walk the user through creating a server-side auto-forward rule to their alias (Gmail Settings→Forwarding / filter; Outlook rule). This is the ingestion path — manual "Fwd" is unsupported/best-effort. Show copy-paste instructions + the alias.
- [ ] On account deletion: call `DELETE /users/{external_user_id}/data`
- [ ] Expose webhook endpoint `POST /webhooks/email-extractions`:
  - Verify `X-EmailBudget-Signature` + `X-EmailBudget-Timestamp`
  - `extraction.created` → queue for user notification
  - `extraction.failed` → notify "couldn't read email, enter manually"
- [ ] Transaction review UI:
  - `GET /extractions/{id}` for full detail; parse `amount` as a **decimal string** (not a float)
  - Editable form pre-filled with extracted fields
  - Show `extraction_confidence` (high/low_confidence badge) and `duplicate_confidence` as UI badges
  - Show raw snippet from `GET /extractions/{id}/preview`
  - On confirm: `POST /extractions/{id}/confirm {category}` → then `POST /feedback/category {merchant_normalized, category_confirmed}`
  - Store `email_extraction_id` on the budgeting app's `Transaction`
- [ ] Display forwarding alias in user settings UI
- [ ] Map email API `category_suggestion` strings to budgeting app category names

---

## Future Enhancements (Post-MVP)

> From [docs/architecture/redesign-summary.md](docs/architecture/redesign-summary.md) Q3 and [docs/future-roadmap/roadmap.md](docs/future-roadmap/roadmap.md)

### Phase 2 — Extraction Intelligence

- [ ] Fuzzy duplicate detection with `pg_trgm` (handles "Amazon" vs "AMZN MKTP") — introduces the 0.60–0.99 confidence band; keep suppress-only-exact, flag the rest
- [ ] Bulk re-extraction after template improvement
- [ ] Multiple aliases per user (one per card/account)
- [ ] `GET /admin/top-failing-senders` — drives new template development
- [ ] Template degradation alerts (Sentry when failure rate > 20% on a template)
- [ ] Multi-transaction email support (statements with multiple rows)
- [ ] Extraction accuracy dashboard (time-series)
- [ ] Full GDPR data export (JSON + CSV packaging)
- [ ] **Auto-confirm policy:** flip `auto_approve_threshold` on — auto-confirm high-confidence extractions (optionally only after N confirmed from that template) to cut review friction for high-volume users
- [ ] P2 sender templates: shopify.com, squareup.com, etsy.com, and user-submitted failures
- [ ] **Manual-forward support:** promote body-embedded sender resolution from best-effort to a supported tier once auto-forward volume is proven

### Phase 2 — Budgeting App Category Integration

- [ ] `POST /config/categories` — accept the budgeting app's taxonomy
- [ ] Suggest using real budgeting app category IDs (not generic strings)
- [ ] Per-user heuristic rules (separate from system-wide)
- [ ] Confidence threshold configurable per budgeting app instance

### Phase 3 — AI Fallback

- [ ] Claude Haiku as Stage 3 fallback (only when template AND regex produce zero fields)
- [ ] Strict gating: AI confidence < 0.85 → still `extraction_failed`
- [ ] Store `method=ai` for auditability
- [ ] A/B: AI fallback vs. failure rate (net accuracy gain or noise?)
- [ ] Batch API for non-urgent extractions; prompt caching on the system prompt

### Phase 3 — Inbox Connection (OAuth)

- [ ] Gmail OAuth (Nylas or direct Gmail API); Outlook via Microsoft Graph
- [ ] Scheduled inbox scanning; provider webhook for real-time events
- [ ] 30-day historical scan on first connection; disconnect + delete controls

### Phase 3 — Reliability

- [ ] Migrate to Celery + Redis only if pipeline tasks exceed ~30s or need task inspection
- [ ] Cloudflare Queue DLQ monitoring + re-queue UI
- [ ] Grafana + Loki for structured logs and alerting

### Phase 4 — Enrichment

- [ ] PDF attachment extraction (pypdf / AWS Textract)
- [ ] OCR for image-only receipts
- [ ] Merchant logo fetching
- [ ] Recurring transaction detection
- [ ] Custom domain support for forwarding addresses

### Phase 5 — Platform

- [ ] Public API for external budgeting apps
- [ ] Multi-tenant / team accounts; SSO/SAML
- [ ] Self-hosted option with local LLM
- [ ] Community template library

---

*This plan is the source of truth for MVP work. Update task status as work progresses.*
*Architecture decisions should update the relevant [docs/](docs/) file first, then this plan.*
