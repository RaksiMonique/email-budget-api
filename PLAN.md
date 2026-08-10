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
7. **Dedup suppresses only exact matches** (`duplicate_confidence = 1.0`); anything less is flagged, never suppressed — matches the "0% false suppression" target. *(Superseded 2026-08-05: even exact matches are now **flag-only** — day-granularity fingerprints collide on legitimate identical same-day purchases, so nothing is ever auto-suppressed; see Phase 5.)*
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
- [x] ⛅ Render account confirmed (2026-08-05) — **Phase 0 fully complete.**
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
>
> **Update (2026-08-10):** **forward-unwrapping implemented** — a *manually*-forwarded bank alert destroys the bank's DKIM (becomes the forwarder's gmail DKIM) and buries the real sender in the quoted body; `sender_resolver` now detects a consumer-provider outer sender and recovers the original from the "Begin forwarded message" block (closes the #1 risk from the original review). **First real validated template: `jncb.com` (NCB Jamaica)** — proven end-to-end against a real alert (synthetic fixture, PII sanitized): `jncb.com` → `bank_alert` → `JMD 3750.00 / merchant / card / date` → `pending_review` high. `chase.com` remains synthetic-only. Body-`From` trust is spoofable → tracked by `VERIFY_SENDER_AUTH` in the security backlog (fine for MVP: flag-only, no auto-confirm).

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
- [x] 🔧 **Forwarding-verification detection** (`verification_detector.py`, added 2026-07-28): Gmail's confirm-your-forwarding-address email lands at the alias, not in any inbox — detect it by exact sender (`forwarding-noreply@google.com`, checked *before* the financial registry, which would misread google.com as a receipt), extract code + confirmation URL, route to `status=forwarding_verification`. Service layer surfaces it via a `forwarding.verification` webhook (Phase 7) so the budgeting app can show the code during onboarding. Deliberately **no auto-confirm** — clicking the URL server-side would let a stranger wire *their* inbox to a victim's alias; a human stays in the loop.

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

- [x] ⛅ Enable Cloudflare Email Routing on the domain (zone status `ready`; `fintrack.` subdomain already enabled — MX present)
- [x] ⛅ Confirm the required MX records (`dig MX fintrack.raksimoni.com` → route1/2/3.mx.cloudflare.net ✓, verified 2026-07-28)
- [x] ⛅ SPF TXT present on `fintrack.`: `"v=spf1 include:_spf.mx.cloudflare.net ~all"` ✓
- [x] ⛅ Zone catch-all → `email-ingest-worker` (set via API 2026-07-28; behavior-preserving: explicit personal rules win, unknown mail still rejected — now by the Worker)

### Cloudflare Queues

- [x] ⛅ Create queue: `email-processing` (done early, 2026-07-27 — id `9582781646144c2c882ca4e4314f44c7`)
- [x] ⛅ Create dead-letter queue: `email-processing-dlq` (id `83dfba16d45e4c7eaf017dd46529e69f`)
- [x] 🔧 Note queue IDs for `wrangler.toml` (recorded above)

### Email Worker (`workers/email-ingest`)

- [x] 🔧 Create `workers/email-ingest/` (`wrangler.toml`, `src/index.ts`)
- [x] 🔧 Implement Email Worker:
  - Receive `email` event; derive `alias_hash` from the recipient
  - **Validate alias at the edge FIRST:** `GET /internal/aliases/{alias_hash}` with `X-Internal-Secret`, result cached in the Workers Cache API / KV (short TTL). Unknown or inactive alias → **drop silently, do NOT write R2, do NOT enqueue.**
  - Only then: read raw MIME → `ArrayBuffer` → upload to R2 `emails/{alias_hash}/{email_id}.eml`
  - Push minimal message to `email-processing`:
    `{ email_id, alias_hash, r2_key, from, to, subject, message_id, date_header, received_at }`
- [x] ⛅ Deploy Email Worker (`wrangler deploy` 2026-07-28, version `40c4756a`; `workers_dev=false` — no public HTTP surface)
  - Edge alias validation is **fail-open until FastAPI exists** (explicit 404/410 → reject; API absent/erroring → accept, webhook re-validates); alias *shape* (8–64 url-safe chars) is enforced at the edge now
- [x] ⛅ Bind R2 bucket + Queue (via `wrangler.toml`; confirmed in deploy output: `R2_BUCKET` → email-budget-raw, `EMAIL_QUEUE` → email-processing)
- [x] 🔧 Manual test (live, 2026-07-28): real Gmail → `k3PZx9WqL2mN8vTa@fintrack…` → `.eml` in R2 (7 KB, alias lowercased) ✓; invalid `x@fintrack…` rejected at edge (no R2 object) ✓; `.eml` pulled into the Phase 1 corpus and run through the local pipeline — DKIM resolution (`d=gmail.com`, 0.97) and non-financial classification both correct ✓

### Queue Consumer Worker (`workers/email-queue-consumer`)

- [x] 🔧 Create `workers/email-queue-consumer/` (`wrangler.toml`, `src/index.ts`)
- [x] 🔧 Implement Consumer Worker:
  - Consume `email-processing` (batch size 10)
  - `POST /internal/email-received` to FastAPI with `X-Internal-Secret`
  - `message.ack()` on 200; `message.retry()` on non-200 (FastAPI processes synchronously, so 200 means fully stored — see Phase 3)
- [x] ⛅ Set Worker env: `FASTAPI_INTERNAL_URL` (var) + `INTERNAL_SECRET` (secret) — done 2026-08-08 in Phase 9 wiring
- [x] ⛅ Deploy Consumer Worker + bind queue as consumer (2026-08-08; queue shows 1 consumer). Was correctly deploy-gated until FastAPI existed.

**Phase 2 complete when:** auto-forwarding to a *known* alias produces an R2 object + a queue message, and forwarding to an *unknown* alias is dropped at the edge.

> **Status: ✅ complete 2026-07-28** (with the documented caveat that edge alias validation is shape-only + fail-open until FastAPI exists; registry-backed edge rejection activates in Phase 9 wiring). Consumer Worker deploy is deliberately deferred — see gate note above.

---

## Phase 3 — FastAPI Foundation

> Goal: the internal webhook validates, dedupes, and creates records — the seam the Phase 1 pipeline plugs into.
> Docs: [docs/architecture/stack-decisions.md](docs/architecture/stack-decisions.md)

### Project setup

- [x] 🔧 Create `backend/`
- [x] 🔧 `pyproject.toml` deps (sentry via optional extra; installed into `backend/.venv`)
- [x] 🔧 `app/config.py` (pydantic-settings; **rewrites `postgres://` → `postgresql+asyncpg://`** for Render)
- [x] 🔧 `app/main.py` (app factory, routers, optional Sentry init, `/healthz`; outbox poller lands in Phase 7)
- [x] 🔧 `docker-compose.yml` for local PostgreSQL (postgres:16 + healthcheck)

### Database schema

- [x] 🔧 Alembic set up with async `env.py`; initial migration `71d4bcb7ea26` generated + applied against dockerized postgres (14 tables incl. `alembic_version`)
- [x] 🔧 SQLAlchemy models:
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
- [x] 🔧 `alembic revision --autogenerate -m "initial_schema"` → `alembic upgrade head` ✓
- [x] 🔧 Index `extraction_results.fingerprint` and `extraction_results.external_user_id` (+ partial unique `(alias_hash, message_id)` on `imported_emails` for dedup)

### Authentication middleware

- [x] 🔧 `app/security/api_key.py` — `X-API-Key`, sha256 + indexed lookup, `Depends(require_api_key)`, `last_used_at` stamped
- [x] 🔧 `app/security/internal_secret.py` — `X-Internal-Secret` via `secrets.compare_digest` (rejects when unset)
- [x] 🔧 API key seeding CLI: `python -m app.seed.create_api_key <label>` (prints key once, stores hash)

### Alias management endpoints

- [x] 🔧 `POST /api/v1/aliases` — `secrets.token_urlsafe(12).lower()` (~77 bits post-case-fold; lowercase because the Worker lowercases recipients), uniqueness-checked
- [x] 🔧 `GET /api/v1/aliases?external_user_id=` — list user's aliases
- [x] 🔧 `GET /api/v1/aliases/{id}` — alias detail incl. `emails_received` counter (increments on **every** accepted email regardless of classification — the onboarding "waiting for first email" poll target)
- [x] 🔧 `DELETE /api/v1/aliases/{id}` — deactivate (`is_active=false`)
- [x] 🔧 `GET /internal/aliases/{alias_hash}` — edge validation for the Email Worker (200 active / 410 deactivated / 404 unknown)

### Internal webhook endpoint (synchronous)

- [x] 🔧 `POST /internal/email-received`:
  - Validate `X-Internal-Secret`
  - Validate alias exists & active (defense in depth; the Worker already checked at the edge)
  - `message_id` dedup — if already processed, return `200` immediately (idempotent)
  - Create `ImportedEmail` (status=`received`)
  - **Run the full pipeline synchronously** (Phase 4): parse → resolve sender → classify → extract → score → store
  - Write the outbound event to `webhook_outbox` in the *same transaction* (Phase 7)
  - Return `200` **only after commit**. On any exception → return `5xx` so Cloudflare Queue retries. Reprocessing is safe (keyed on `message_id`, stable `r2_key`).
  - ❌ **No `asyncio.create_task`** — that fire-and-forget pattern was a Postmark-30s-timeout fossil; Cloudflare Queues are patient and give retry for free.

### R2 client

- [x] 🔧 `app/integrations/r2_client.py` — async-wrapped boto3; `R2ObjectMissing` (permanent) distinguished from transient errors

**Phase 3 complete when:** POSTing a fixture payload to `/internal/email-received` runs the real pipeline and leaves an `ExtractionResult` (or `extraction_failed`) row + a `webhook_outbox` row, all before the 200.

> **Status: ✅ complete 2026-07-28.** 15/15 tests green against real dockerized Postgres — full webhook flow (extraction + `extraction.created` + `alias.first_email_received` outbox rows, `emails_received=1`), duplicate idempotency, unknown-alias ack-and-drop, edge check 200/404, API-key + internal-secret auth, alias lifecycle. Bonus beyond plan: `alias.first_email_received` event wired; forwarding-verification emails enqueue `forwarding.verification`.
>
> **Adversarial review (23-agent workflow, 2026-07-28): 10 findings → 7 confirmed → all fixed** (+ regression tests + migration `47feee03cd82`):
> 1. **r2_key is now the primary idempotency key** (unique index) — emails without a Message-ID no longer double-import on queue retry-after-commit; message_id dedup stays as the re-forward catch.
> 2. Queue-payload strings clamped to column limits (message_id 998, from 320, r2_key 512) — an over-length header degrades instead of poisoning the message into the DLQ via DataError→500 loops.
> 3. Out-of-range amounts (≥10^11) degrade to `extraction_failed` — attacker-deliverable "USD 999999999999.99" can no longer DLQ-poison a message.
> 4. `last_used_at` touch commits inside `require_api_key` — was silently rolled back on read-only routes (breaking key-rotation decisions) and held a row lock per request.
> 5. Production serves no `/openapi.json`, `/docs`, or `/redoc` (was: docs off but schema exposed).
> 6. Alias row locked `FOR UPDATE` during processing — serializes concurrent deliveries; kills the duplicate `alias.first_email_received` race (contested finding, fixed anyway — one line).
> 7. Sentry events scrub `X-API-Key`/`X-Internal-Secret` headers (`before_send`) — custom auth headers are not in Sentry's default denylist (contested finding, fixed preemptively).

---

## Phase 4 — Service Integration

> Goal: wire the proven Phase 1 pipeline behind the Phase 3 webhook, reading real bytes from R2.

- [x] 🔧 `app/services/extraction_service.py` — orchestrates: fetch `.eml` from R2 → pipeline → persist (built in Phase 3; template `success_count`/`failure_count` update deferred to template work)
- [x] 🔧 `Decimal` flows end-to-end (asserted in tests; amounts serialized as strings in outbox payloads)
- [x] 🔧 **Local e2e replay (2026-08-04):** booted the API locally and replayed both real R2 emails through `/internal/email-received` with real R2 fetches — `non_financial` + `forwarding_verification` rows, `forwarding.verification` outbox event carrying the real Google confirmation URL, one-time `alias.first_email_received`, `emails_received=2`, live duplicate replay → `duplicate:true`

**Phase 4 complete when:** auto-forwarding a real bank alert end-to-end (Cloudflare → R2 → queue → FastAPI) produces a populated `ExtractionResult` with `email_type=bank_alert`.

> **Status: core proven locally 2026-08-04** (R2 → pipeline → rows → outbox, all through the live service). Remaining for full completion: deploy FastAPI (Phase 9) + Consumer Worker, then a real bank alert through the Cloudflare path — also pending: any alert-shaped emails arriving at the alias (none in R2 yet as of 2026-08-04).

---

## Phase 5 — Duplicate Detection

> Docs: [docs/duplicate-detection/duplicate-detection.md](docs/duplicate-detection/duplicate-detection.md)

- [x] 🔧 `app/services/duplicate_service.py` (built 2026-08-04):
  - Email-level dedup: `r2_key` (primary) + alias-scoped `message_id` in the webhook handler
  - Transaction-level dedup:
    - [x] Fingerprint: SHA-256 over minor-unit amount + currency + normalized merchant + date (computed in the pure pipeline)
    - [x] Query scoped to `external_user_id`; matches only **live** rows (`pending_review`/`confirmed`) — a dismissed/failed earlier row is not evidence of duplication
    - [x] **FLAG-ONLY (policy revised 2026-08-05):** exact fingerprint match → `duplicate_confidence = 1.0` + `DuplicateMatch` row + badge in the webhook payload, **status stays `pending_review`** — the user resolves it in the budgeting app
    - **Why no auto-suppression at all:** the fingerprint has *day* granularity, so two legitimate identical same-day purchases (two transit taps, two coffees) collide — auto-suppressing would silently lose real transactions, violating the "0% false suppression" success criterion. The review fleet's skeptics initially killed this finding as "working as designed"; overridden on the metric's plain reading. Auto-suppression may return in Phase 2 with stronger evidence (card_last4 + time proximity).
    - Fuzzy matching (`pg_trgm`, the 0.60–0.99 band) is Phase 2 — also flag-only

**Phase 5 status: ✅ complete 2026-08-05 (flag-only)** — tested: same transaction via two different emails flags the second (both live, both webhook events, badge on the flagged one); dismissed earlier row does *not* flag.

---

## Phase 6 — Extraction Results API

> Docs: [docs/api/api-skeleton.md](docs/api/api-skeleton.md)

- [x] 🔧 `GET /api/v1/extractions` — paginated; filter by `external_user_id`, `status`, date range
- [x] 🔧 `GET /api/v1/extractions/{id}` — full detail incl. `field_confidences`, `duplicate_matches` (amounts as **canonical** decimal strings — `normalize()`d so DB scale-padding never leaks)
- [x] 🔧 `GET /api/v1/extractions/{id}/preview` — fields + per-field raw snippets
- [x] 🔧 `POST /api/v1/extractions/{id}/confirm` — idempotent (repeat returns same result, doesn't clobber category); confirm-after-dismiss → 409
- [x] 🔧 `POST /api/v1/extractions/{id}/dismiss` — with reason; idempotent; dismiss-after-confirm → 409
- [x] 🔧 `POST /api/v1/extractions/{id}/reprocess` — re-fetch from R2, re-run pipeline, update in place via the **same field-mapper as initial persistence** (can't drift); confirmed/dismissed rows protected (409)
- [x] 🔧 `POST /api/v1/feedback/category` — 3+ same-merchant/category confirmations → exact-match `merchant_rules` row (priority 50, outranks seeds). *Known seam: DB rules are not yet consumed by the pure pipeline (seed constants only) — wire in Phase 2 alongside rule caching.*
- [x] 🔧 `GET /api/v1/stats/extraction?external_user_id=` — totals by status, success rate, top senders, top failing senders

**Phase 6 status: ✅ complete 2026-08-04** — full lifecycle tested (list/detail/preview/confirm-idempotent/409s/reprocess/feedback-rule-creation/stats).

---

## Phase 7 — Outbound Webhook (Outbox Pattern)

> Docs: [docs/api/webhook-strategy.md](docs/api/webhook-strategy.md)

- [x] 🔧 `POST /api/v1/config/webhook` — stores URL + secret **encrypted at rest** (Fernet via `SECRET_ENCRYPTION_KEY`; rotation invalidates stored config — re-POST after rotating)
- [x] 🔧 `POST /api/v1/config/webhook/test` — sends a signed test payload, returns delivery result
- [x] 🔧 `app/services/webhook_delivery_service.py` (built 2026-08-04):
  - Events enqueued transactionally during extraction; never delivered inline
  - Lifespan-started poller claims due rows (`FOR UPDATE SKIP LOCKED`), signs **HMAC-SHA256 over `{timestamp}.{body}`** (`X-EmailBudget-Signature` + `X-EmailBudget-Timestamp`), POSTs; state all in Postgres → redeploy-safe by construction
  - 2xx → `delivered`; failure → backoff immediate → 1m → 5m → 15m → 1h → `failed` (surfaced, never silently lost)
  - No webhook config yet → rows deferred without burning attempts
  - Delivery is **at-least-once** (`event_id` in the envelope for receiver-side idempotency)
- [x] 🔧 `extraction.created` / `extraction.failed` enqueued (Phase 3/4)
- [x] 🔧 `forwarding.verification` enqueued (Phase 3/4)
- [x] 🔧 `alias.first_email_received` enqueued once per alias (Phase 3/4)

**Phase 7 status: ✅ complete 2026-08-05** — tested: secret ciphertext at rest, receiver-side HMAC verification of a real delivery, 5-attempt backoff → `failed`, no-config deferral.

**Adversarial review of Phases 5–7 (25-agent workflow, 2026-08-05): 11 findings → 5 confirmed → all fixed, +1 killed finding overridden** (regression-tested, 31/31 green):
1. **Flag-only dedup** (the override — see Phase 5 rationale)
2. Reprocess now re-runs duplicate flagging, resets stale `DuplicateMatch`/confidence artifacts, updates the email status, and **rejects reclassified emails** (409) instead of writing off-vocabulary statuses like `non_financial` into extraction rows
3. Undecryptable webhook secret (key rotation) → delivery **defers** with a loud log instead of stalling the whole outbox in a 5s error loop
4. Non-`HTTPError` exceptions (e.g. `httpx.InvalidURL`) now fail the **row** with backoff, not the whole batch; config endpoint strictly validates URLs (422 on `http://[::1`-style input)
5. Sentry `include_local_variables=False` — poller exceptions would otherwise ship the **decrypted** webhook secret in stack-frame locals

---

## Phase 8 — Privacy and Data Deletion

> Docs: [docs/security/privacy-compliance.md](docs/security/privacy-compliance.md)

- [x] 🔧 `DELETE /api/v1/users/{external_user_id}/data` (built 2026-08-05):
  - Deactivates all the user's aliases (new mail then drops at the edge/webhook)
  - Schedules R2 deletion: `pending_deletion_at = NOW() + deletion_grace_days` (30) — idempotent, only rows not already scheduled
  - Returns counts; audit-logged
- [x] 🔧 `GET /api/v1/users/{external_user_id}/data-export` — honest 501 stub pointing at Phase 2 (extractions remain queryable via the API meanwhile)
- [x] ⛅→🔧 Deletion executor: **app-level hourly maintenance loop** (lifespan task, same redeploy-safe pattern as the outbox poller) purges grace-expired objects + nulls `r2_key`; the bucket's existing **90-day lifecycle rule (set in Phase 0)** is the independent storage-layer backstop — no separate Cron Worker needed for MVP
- [x] 🔧 Retention sweep in the same loop: `received_at < NOW() - retention_days (90)` → delete R2 object, null `r2_key`; missing objects tolerated (lifecycle may win the race), transient R2 failures retried next sweep
- MVP scope note: raw email content is what's deleted; extraction rows/snippets survive until Phase 2's full GDPR deletion/export

**Phase 8 status: ✅ complete 2026-08-05 (39/39 suite green after review).**

**Adversarial review (12-agent workflow, 2026-08-05): 5 findings → all 5 addressed:**
1. **[the important one] Drop paths orphaned raw emails in R2** — an email arriving *after* a deletion request (or a re-forwarded duplicate) was acked with its stored object left row-less in R2, invisible to every purge query until the 90-day lifecycle. Fixed: r2_key dedup hoisted **above** the alias check (a retry of committed work returns `duplicate` regardless of alias state — which also makes the next part safe), then both drop paths best-effort delete the incoming object no row will ever own. Regression tests cover the post-deletion arrival, the retry-owns-object case, and the re-forward copy.
2. Audit-log counts moved out of the `String(32)` status column (a large user's deletion would 500 + roll back forever) — counts go to logs.
3. R2 network I/O no longer runs under row locks (`FOR UPDATE` dropped; the pointer-null UPDATE is idempotent, so overlapping sweeps are harmless).
4. Purge sweeps now **drain** (up to 50 × 100-row batches per sweep) instead of a hard 100/hour cap that would miss the 30-day promise for large mailboxes.
5. Batch selection is random-ordered so permanently-failing rows can't occupy the same slots every sweep and starve the rest.

---

## Phase 9 — Deployment and Hardening

### Render deployment

- [x] 🔧 `backend/Dockerfile` (built + smoke-tested in-container 2026-08-06: healthz 200; prod mode serves no openapi/docs; caught missing `cryptography` dep in the process)
- [x] 🔧 `render.yaml` Blueprint at repo root (web service + Postgres 16 basic-256mb as code; secrets `sync: false` — entered once in the dashboard)
- [x] ⛅ Render **Web Service** live at **`https://email-budget-api.onrender.com`** (Docker, root dir `backend/`, region Ohio, `/healthz` check) — **FREE tier for now** (spin-down + ~50s cold start accepted for pre-launch testing; upgrade to Starter before real users / the <10s target)
- [x] ⛅ Database is **external Neon** (free, non-expiring), not Render Postgres — migrated + seeded + verified (see DB note below)
- [x] ⛅ Env vars set in Render (`DATABASE_URL`=Neon, R2_*, `INTERNAL_SECRET`, `SECRET_ENCRYPTION_KEY`, `ENVIRONMENT=production`, `RUN_MIGRATIONS=true`)
- [x] 🔧 Migrations run at **container boot** via `RUN_MIGRATIONS` (free tier has no Pre-Deploy Command) — idempotent
- [x] ⛅ Auto-deploy on push to `main` (proven: the `sslmode` fix auto-deployed once pushed)
- [x] ⛅ Consumer Worker `FASTAPI_INTERNAL_URL` → the Render URL (done in Cloudflare section below)

> **Verified from the public internet 2026-08-08:** `/healthz` 200; `/openapi.json` + `/docs` 404 (prod lockdown); authed aliases API + internal edge check return live Neon data; bad key → 401, missing internal secret → 403. Full deployed pipeline proven: a real bank alert POSTed to the deployed `/internal/email-received` → real R2 fetch → Neon `ExtractionResult` (Amazon / USD 45.99 / high / pending_review) read back via the prod API.
>
> **DB note:** Neon direct connection; `db/session.py:prepare_asyncpg_url` strips libpq `sslmode`/`channel_binding` and sets asyncpg `ssl` — the initial Render deploy failed with `connect() got 'sslmode'` until that fix (`50e45b9`) was pushed.

### Cloudflare Workers final deploy

- [x] ⛅ Email Worker redeployed to prod (version `5baed1a5`) + Consumer Worker deployed (version `9de3cfd9`), both wired to the Render URL with `INTERNAL_SECRET`; queue `email-processing` now shows **1 producer + 1 consumer**
- [x] ⛅ Catch-all rule → production Email Worker (set in Phase 2, unchanged)

### Sentry

- [ ] ⛅ Create Sentry project (Python/FastAPI); add SDK init to `app/main.py`
- [ ] ⛅ Sentry on Workers optional — `wrangler tail` for logs in MVP

### Security checklist

- [x] 🔧 `X-Internal-Secret` ≥ 32 random bytes, not in code (`openssl rand -hex 32`, env only)
- [x] 🔧 R2 bucket private (no public access)
- [x] 🔧 API keys stored hashed (SHA-256)
- [x] 🔧 Webhook secret encrypted at rest (Fernet)
- [x] 🔧 Alias tokens ≥ 72 bits (`secrets.token_urlsafe(12)`)
- [x] 🔧 All SQL parameterized (SQLAlchemy ORM — no raw f-strings)
- [ ] 🔧 Rate limiting on `/internal/email-received` — **NOT built; known gap** → see "Security hardening (leaked-alias abuse)" under Future Enhancements
- [x] 🔧 `secrets.compare_digest` for internal-secret + HMAC comparisons
- [x] 🔧 Amounts serialized as decimal strings (never JSON floats) across all API + webhook payloads

---

## Phase 10 — End-to-End Validation

> Use **auto-forwarded** fixtures. Manual forwards are a best-effort tier and may fail — validate them separately, don't gate MVP on them.

- [ ] Auto-forward a Chase bank alert → `email_type=bank_alert`, amount/merchant/date populated
- [ ] Auto-forward an Amazon receipt → `ExtractionResult` with amount, merchant, date
- [ ] Auto-forward a non-financial email (newsletter) → `status=non_financial`, no extraction
- [ ] Auto-forward the same email twice → second is dropped by email-level dedup (`r2_key`/`message_id`, `duplicate: true`); same *transaction* via two different emails → second flagged `duplicate_confidence=1.0`, still `pending_review`
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
| Duplicate false suppression rate (unique tx suppressed) | 0% **by construction** — flag-only dedup, nothing auto-suppressed in MVP |
| Webhook delivery within 1 hour (outbox-backed) | > 99% |

> Note: banks + merchants are in MVP scope. Bank alerts are short and highly templated; merchant receipts (line items, tax, tips, split shipments) are harder — expect them to consume most of the accuracy budget on the ≥95% target.

---

## Budgeting App Integration Tasks
> Tasks in the budgeting app codebase, not this one.
> Full contract: [docs/integration/budgeting-app-integration.md](docs/integration/budgeting-app-integration.md)

- [ ] Store Email API key securely in budgeting app config
- [ ] On user onboarding (email feature enabled): call `POST /aliases` → store returned alias
- [ ] **Auto-forward setup UX:** walk the user through creating a server-side auto-forward rule to their alias (Gmail Settings→Forwarding / filter; Outlook rule). This is the ingestion path — manual "Fwd" is unsupported/best-effort. Show copy-paste instructions + the alias.
- [ ] **Closed-loop Gmail verification:** subscribe the onboarding screen to `forwarding.verification` webhooks — when Gmail sends its confirmation email to the alias, display the extracted code/link inline ("Your Gmail code: XXXXX") so the user finishes setup without ever needing an inbox at the alias. Show the confirmation link for the *user* to click — never auto-confirm server-side.
- [ ] **"Waiting for your first email" indicator (all providers, incl. non-verifying ones like Outlook):** while the onboarding screen is open, poll `GET /aliases/{id}` every ~5s; `emails_received > 0` → flip to "✓ Forwarding works!". Prompt the user to **send themselves a test email matching their filter** so the loop closes in seconds instead of waiting days for a real bank alert. (Optionally subscribe to `alias.first_email_received` instead of polling.)
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

### Security hardening — leaked-alias abuse (post-launch)

> **Threat (identified 2026-08-10):** the alias is an unguessable bearer token (~72 bits), but if it *leaks* — it appears in the user's Sent folder, forwarding settings, and the app's settings UI — anyone who learns it can send unlimited mail to that user's alias. Blast radius is bounded (user-scoped; **no auto-confirm**, so fakes can only clutter the `pending_review` queue, never the real ledger), but the flood + spoof vectors below are real and currently unmitigated.

- [ ] 🔧 **Per-alias rate limiting on `/internal/email-received`** (the "flood" gap — the Phase 9 checklist listed this but it was never built). Cap accepted emails per alias per window (e.g. N/hour); excess → drop silently (ack 200, no R2 write, no row). A DB-count check is fine for the single-instance MVP (count `imported_emails` for the alias in the window); a dedicated counter/table if it needs to scale. Drop at the Email Worker edge too, to avoid the R2 write.
- [ ] 🔧 **Sender-authentication verification — env-configurable** via **`VERIFY_SENDER_AUTH`** (bool in `config.py`, default `false`). Today [sender_resolver.py](backend/app/extraction/sender_resolver.py) regex-extracts the DKIM `d=` domain but **never verifies the signature** — so an attacker who knows the alias can forge `From: alerts@chase.com` + a fake `DKIM-Signature: …d=chase.com` and manufacture a convincing fake `pending_review` "Chase transaction."
  - **ON** → only trust a resolved sender domain for template matching / financial-registry classification if the message *authenticated* for that domain: read Cloudflare Email Routing's `Authentication-Results` header (the edge already runs SPF/DKIM/DMARC), or cryptographically verify DKIM.
  - **OFF** (default for now) → current permissive behavior — needed because manual/edge-case forwarding can break strict alignment, which is exactly why it must be a toggle, not hardcoded.
  - Tension to validate: legitimate *auto*-forwarded bank mail keeps valid original DKIM, so ON should pass real bank alerts while rejecting spoofs — confirm against the real corpus before defaulting ON in prod.
- [ ] 🔧 **Alias rotation endpoint** (`POST /api/v1/aliases/{id}/rotate` — deactivate the leaked alias + mint a new one for the same `external_user_id`, returned to the budgeting app). Recovery path when an alias leaks; today only the manual deactivate-old + create-new sequence exists.
- [ ] 🔧 Optional: per-alias daily caps on R2 objects / `emails_received` as a storage-abuse backstop, surfaced in `import_audit_logs`.

> Run all of these through the same adversarial-review Workflow pass as the other phases before committing.

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

### Phase 2 — User-Managed Financial Sender Registry (DB-backed)

> Moves `FINANCIAL_SENDER_REGISTRY` (today a hardcoded dict in `app/seed/financial_senders.py`) into the database so senders can be managed per user and learned over time. Companion to templates graduating to the `extraction_templates` table.

**Product approach (recommended): the alias IS the opt-in — parse everything that arrives, and learn senders *reactively* by confirmation, not pre-declaration.** Users forward mail to a dedicated alias, so "is this financial?" is largely pre-answered by the act of forwarding. And users don't know their bank's sender domain (real example: `jncb.com` with a `no-reply-ncbcardalerts@` local part — not `ncb.com`). So instead of asking them to declare domains, surface the *observed* sender for one-tap confirmation: *"New sender `no-reply-ncbcardalerts@jncb.com` — track as financial? [Yes, my bank] / [No]."* Proactive declaration stays at the **human** level (onboarding: "which banks/cards?") to set expectations + guide the forwarding-rule setup; the domain registry is *learned*. Same philosophy as the existing merchant→category feedback loop — confirmations teach the system, no AI.

**Schema — two tables replace the constant:**
- [ ] 🔧 `financial_senders` (global, curated + community): `domain` (unique), `email_type`, `display_name`, `has_template` (bool), `scope` (`system` | `community`), `confirmation_count`, `is_active`. Seeded from today's constant as `scope=system`.
- [ ] 🔧 `user_sender_prefs` (per-user): `external_user_id`, `domain`, `email_type`, `action` (`include` | `exclude`), `source` (`declared` | `confirmed_on_arrival`), unique(`external_user_id`, `domain`). `exclude` lets a user opt a domain out (e.g. "don't track my Amazon receipts") without affecting anyone else.

**Classification** (DB-backed, cached like `merchant_rules` — 10-min TTL, invalidate on write):
- [ ] 🔧 resolve sender → user `exclude`? → `non_financial`; else user `include` **or** global `financial_senders` hit → financial with that `email_type`; else subject-pattern fallback → financial(`unknown`) **+ a `sender_status: candidate` flag**; else `non_financial`.
- [ ] 🔧 extraction results / webhooks expose the resolved sender + `sender_status` so the budgeting app can raise the "track this sender?" prompt reactively.

**API** (budgeting-app-facing, X-API-Key):
- [ ] 🔧 `GET /api/v1/senders?external_user_id=` — merged view (global + user include/exclude)
- [ ] 🔧 `POST /api/v1/senders` — user adds/confirms a sender (writes `user_sender_prefs`, `source=confirmed_on_arrival`)
- [ ] 🔧 `DELETE /api/v1/senders/{id}` — remove a user pref

**Learning / community promotion** (mirrors the 3+-confirmations merchant-rule loop):
- [ ] 🔧 a `confirmed_on_arrival` increments the global `confirmation_count`; after **N distinct users** confirm the same domain → auto-promote to `financial_senders` (`scope=community`), so users collectively discover new banks with no admin.

**Template coupling (keep honest):** recognizing a sender as financial ≠ being able to extract it. A user-added sender with **no template** classifies as its type, but the general extractor can't get a merchant → `extraction_failed`. So user-added senders are **template-wanted signals** feeding `GET /admin/top-failing-senders` (drives which template to build next). Sender management and template-building are two halves of one loop.

**Migration / phasing:**
- [ ] 🔧 **2a** — Alembic tables + seed `financial_senders` from the constant on deploy; `classification_service` reads the cache instead of the dict. **No behavior change** (the constant stays the seed source of truth).
- [ ] 🔧 **2b** — per-user CRUD API + `user_sender_prefs` include/exclude
- [ ] 🔧 **2c** — reactive `confirmed_on_arrival` + community promotion

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
