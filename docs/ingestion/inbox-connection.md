# Inbox Connection Module

> 🔮 **Phase 3 (deferred) — and predates the redesign.** Inbox OAuth is out of MVP scope, and this design assumes removed infra (Clerk JWT, Redis OAuth state). Revisit when Phase 3 begins. MVP scope: [PLAN.md](../../PLAN.md).

## Overview

Inbox connections allow users to authorize the system to scan their Gmail or Outlook inbox for financial emails. The connection is read-only, scoped to financial content, and revocable at any time. Nylas provides a unified OAuth API across both providers.

---

## OAuth Providers Supported

| Provider | Nylas Auth Type | Email API | Notes |
|----------|----------------|-----------|-------|
| Google (Gmail) | Google OAuth 2.0 | Gmail API v1 | `gmail.readonly` scope |
| Microsoft (Outlook) | Microsoft OAuth 2.0 | Microsoft Graph API v1 | `Mail.Read` scope |

Both are abstracted through Nylas — the application never calls Gmail API or Graph API directly.

---

## Connection Lifecycle

```
[not connected]
      │ user clicks "Connect Gmail"
      ▼
[oauth_in_progress]   ← state stored in Redis, 10-minute TTL
      │ user completes OAuth consent
      ▼
[active]              ← Nylas grant stored, encrypted
      │ token expires or user revokes externally
      ├──→ [expired] / [needs_reauth]
      │ user clicks "Disconnect"
      ▼
[revoked]             ← Nylas grant revoked, token deleted
```

---

## OAuth Flow — Step by Step

### Step 1: Generate Authorization URL

```
GET /api/v1/inbox-connections/auth-url?provider=google
Authorization: Bearer <clerk_jwt>
```

```python
async def generate_auth_url(user_id: UUID, provider: str) -> AuthUrlResponse:
    # Generate CSRF state token
    state = secrets.token_urlsafe(32)
    
    # Store in Redis: expires in 10 minutes
    await redis.setex(
        f"oauth_state:{state}",
        600,
        json.dumps({"user_id": str(user_id), "provider": provider})
    )
    
    # Generate Nylas OAuth URL
    auth_url = nylas.auth.url_for_oauth2({
        "client_id": settings.NYLAS_CLIENT_ID,
        "redirect_uri": settings.OAUTH_CALLBACK_URL,
        "scopes": ["email.readonly"],
        "provider": provider,  # "google" or "microsoft"
        "state": state,
    })
    
    return AuthUrlResponse(auth_url=auth_url, state=state)
```

**Response:**
```json
{
  "auth_url": "https://api.us.nylas.com/v3/connect/auth?client_id=...&state=...",
  "state": "csrf_state_token"
}
```

Frontend redirects user to `auth_url`. User sees Google/Microsoft consent screen requesting `email.readonly` access.

---

### Step 2: OAuth Callback

```
GET /api/v1/inbox-connections/callback?code=4/XYZ&state=csrf_state_token
```

```python
async def handle_oauth_callback(code: str, state: str, request: Request):
    # 1. Validate state (CSRF protection)
    state_data = await redis.get(f"oauth_state:{state}")
    if not state_data:
        raise HTTPException(400, "Invalid or expired OAuth state")
    
    state_obj = json.loads(state_data)
    await redis.delete(f"oauth_state:{state}")  # one-time use
    
    user_id = UUID(state_obj["user_id"])
    provider = state_obj["provider"]
    
    # 2. Exchange code for Nylas grant
    try:
        token_response = nylas.auth.exchange_code_for_token({
            "client_id": settings.NYLAS_CLIENT_ID,
            "client_secret": settings.NYLAS_CLIENT_SECRET,
            "redirect_uri": settings.OAUTH_CALLBACK_URL,
            "code": code,
        })
    except NylasAPIError as e:
        # Redirect to frontend with error
        return RedirectResponse(f"{settings.FRONTEND_URL}/settings/inboxes?error=oauth_failed")
    
    grant_id = token_response.grant_id
    access_token = token_response.access_token
    connected_email = token_response.email
    
    # 3. Check if this email is already connected
    existing = await db.get_connection_by_email(user_id, connected_email)
    if existing and existing.status == "active":
        return RedirectResponse(
            f"{settings.FRONTEND_URL}/settings/inboxes?error=already_connected"
        )
    
    # 4. Encrypt access token
    encrypted_token, token_iv = encrypt_token(access_token, user_id)
    
    # 5. Create InboxConnection record
    connection = InboxConnection(
        user_id=user_id,
        provider=provider,
        nylas_grant_id=grant_id,
        connected_email=connected_email,
        encrypted_access_token=encrypted_token,
        token_iv=token_iv,
        status="active",
    )
    await db.save(connection)
    
    # 6. Audit log
    await audit_log(user_id, "inbox_connected", {
        "provider": provider,
        "connected_email": mask_email(connected_email),
    })
    
    # 7. Enqueue initial historical scan
    initial_scan_task.delay(
        connection_id=str(connection.id),
        lookback_days=30,
    )
    
    # 8. Redirect to frontend with success
    return RedirectResponse(
        f"{settings.FRONTEND_URL}/settings/inboxes?connection_id={connection.id}&status=success"
    )
```

---

### Step 3: Initial Historical Scan

Runs asynchronously after OAuth connection. Scans the last 30 days for financial emails.

```python
@celery_app.task(bind=True, queue="inbox_scan")
def initial_inbox_scan(self, connection_id: str, lookback_days: int = 30):
    connection = db.get(InboxConnection, connection_id)
    if not connection or connection.status != "active":
        return
    
    job = create_import_job(
        user_id=connection.user_id,
        connection_id=connection.id,
        job_type="initial_scan",
        lookback_days=lookback_days,
    )
    
    since_date = datetime.utcnow() - timedelta(days=lookback_days)
    
    scan_inbox_since(connection, since_date, job)
```

---

### Step 4: Scheduled Incremental Scans

Celery Beat triggers `scan_all_active_connections` every 15 minutes.

```python
@celery_app.task(queue="inbox_scan")
def scan_all_active_connections():
    """Dispatch individual scan tasks for all active connections."""
    connections = db.query(InboxConnection).filter_by(status="active").all()
    for connection in connections:
        scan_inbox_task.apply_async(
            args=[str(connection.id)],
            countdown=0,
        )

@celery_app.task(bind=True, queue="inbox_scan", max_retries=3)
def scan_inbox_task(self, connection_id: str):
    connection = db.get(InboxConnection, connection_id)
    if not connection or connection.status != "active":
        return
    
    # Distributed lock: prevent concurrent scans on same connection
    lock_key = f"scan_lock:{connection_id}"
    with redis.lock(lock_key, timeout=1800, blocking_timeout=5) as lock:
        if not lock:
            return  # another worker is already scanning this connection
        
        since = connection.last_scanned_at or (datetime.utcnow() - timedelta(hours=1))
        scan_inbox_since(connection, since)
        
        connection.last_scanned_at = datetime.utcnow()
        db.commit()
```

---

## Nylas API Integration

### Fetching Emails

```python
def fetch_financial_emails(
    connection: InboxConnection,
    since: datetime,
    until: Optional[datetime] = None,
) -> Iterator[NylasEmail]:
    """
    Fetch emails from Nylas API with financial filters.
    Returns iterator to support large mailboxes without loading all into memory.
    """
    access_token = decrypt_token(
        connection.encrypted_access_token,
        connection.token_iv,
        connection.user_id
    )
    
    nylas_client = nylas.Messages(access_token, connection.nylas_grant_id)
    
    # Build filter: fetch emails from financial senders OR with financial subjects
    # Nylas supports 'any' / 'from' / 'subject' filters
    query_params = {
        "received_after": int(since.timestamp()),
        "limit": 50,  # page size
    }
    if until:
        query_params["received_before"] = int(until.timestamp())
    
    page_token = None
    while True:
        if page_token:
            query_params["page_token"] = page_token
        
        response = nylas_client.list(query_params)
        
        for email in response.data:
            yield email
        
        if not response.next_cursor:
            break
        page_token = response.next_cursor
        
        # Respect Nylas rate limit: 10 requests/second
        time.sleep(0.1)
```

### Financial Email Pre-filter

Nylas doesn't support complex server-side filtering (e.g., "from known financial senders"). We fetch a broader set and filter locally:

```python
def is_potentially_financial(email: NylasEmail) -> bool:
    """
    Quick pre-filter before expensive storage + classification.
    Reduces noise from non-financial inbox emails.
    """
    sender_domain = extract_domain(email.from_[0].email)
    subject = email.subject or ""
    
    # Check sender domain
    if sender_domain in FINANCIAL_SENDER_REGISTRY:
        return True
    
    # Check subject keywords
    financial_keywords = [
        "receipt", "invoice", "payment", "charged", "purchase",
        "order", "transaction", "statement", "alert", "notification",
        "refund", "credit", "debit", "withdrawal", "deposit",
        "subscription", "renewal", "bill", "confirmation"
    ]
    subject_lower = subject.lower()
    if any(kw in subject_lower for kw in financial_keywords):
        return True
    
    return False
```

This pre-filter catches ~95% of financial emails while excluding the bulk of personal/work inbox traffic. The classification module makes the final call.

---

## Nylas Webhook (Real-Time Events)

### Registration

On deployment, register a webhook with Nylas for `message.created` events:

```python
nylas.webhooks.create({
    "trigger_types": ["message.created", "grant.expired"],
    "webhook_url": "https://api.emailbudget.io/webhooks/nylas",
    "description": "Email Budget new email handler",
    "notification_email_address": "admin@emailbudget.io",
})
```

### Handler

```python
@router.post("/webhooks/nylas")
async def nylas_webhook(request: Request):
    body = await request.body()
    
    # Verify HMAC signature
    if not verify_nylas_signature(request, body):
        return {"received": True}  # silent reject
    
    payload = json.loads(body)
    
    for notification in payload.get("data", []):
        event_type = notification.get("type")
        
        if event_type == "message.created":
            grant_id = notification["object"]["grant_id"]
            message_id = notification["object"]["id"]
            
            # Look up connection by Nylas grant_id
            connection = await db.get_connection_by_grant(grant_id)
            if not connection or connection.status != "active":
                continue
            
            # Enqueue fetch + process for this specific message
            fetch_and_process_nylas_message.delay(
                connection_id=str(connection.id),
                nylas_message_id=message_id,
            )
        
        elif event_type == "grant.expired":
            grant_id = notification["object"]["grant_id"]
            connection = await db.get_connection_by_grant(grant_id)
            if connection:
                connection.status = "needs_reauth"
                await db.commit()
                # Notify user: "Your Gmail connection needs re-authorization"
                await notify_user(connection.user_id, "inbox_needs_reauth", {
                    "provider": connection.provider,
                    "email": connection.connected_email,
                })
    
    return {"received": True}
```

---

## Disconnection Flow

```python
async def disconnect_inbox(
    connection_id: UUID,
    user_id: UUID,
    delete_imported_emails: bool = False,
) -> None:
    connection = await db.get_inbox_connection(connection_id, user_id)
    
    # 1. Revoke Nylas grant (revokes Google/Microsoft OAuth token)
    try:
        nylas.grants.destroy(connection.nylas_grant_id)
    except NylasAPIError:
        pass  # continue even if Nylas revocation fails (token may already be invalid)
    
    # 2. Update connection status
    connection.status = "revoked"
    connection.revoked_at = datetime.utcnow()
    connection.encrypted_access_token = None  # delete token from DB
    connection.token_iv = None
    await db.commit()
    
    # 3. Cancel any active scan jobs for this connection
    await cancel_active_scan_jobs(connection_id)
    
    # 4. Optionally delete imported emails
    if delete_imported_emails:
        await purge_connection_emails.delay(
            connection_id=str(connection_id),
            user_id=str(user_id),
        )
    
    # 5. Audit log
    await audit_log(user_id, "inbox_disconnected", {
        "provider": connection.provider,
        "connected_email": mask_email(connection.connected_email),
        "emails_deleted": delete_imported_emails,
    })
```

---

## Connection Health Monitoring

A nightly job checks connection health:

```python
@celery_app.task(queue="maintenance")
def check_connection_health():
    active_connections = db.query(InboxConnection).filter_by(status="active").all()
    for connection in active_connections:
        try:
            # Lightweight Nylas call to test grant validity
            nylas.grants.find(connection.nylas_grant_id)
        except NylasAuthError:
            connection.status = "needs_reauth"
            connection.error_message = "OAuth grant has expired or been revoked"
            db.commit()
            notify_user(connection.user_id, "inbox_needs_reauth", {...})
        except Exception as e:
            connection.status = "error"
            connection.error_message = str(e)[:255]
            db.commit()
```

---

## Edge Cases

| Scenario | Handling |
|----------|----------|
| User revokes Google access externally | Nylas `grant.expired` webhook → mark `needs_reauth` |
| OAuth state expires (> 10 min to complete consent) | State not found in Redis → return `oauth_state_expired` error |
| Same Gmail account connected twice | Detect by `connected_email` → return `already_connected` |
| Nylas API down during scan | Celery retry with exponential backoff; connection stays `active` |
| Initial scan of 10,000 emails | Paginated fetch, progress tracked in ImportJob; cancellable |
| Two scan workers run concurrently | Redis distributed lock prevents double-scanning |
| User deletes account while scan running | Scan job checks user active status before each page |
| Token re-encryption needed (key rotation) | Background job: decrypt old key, re-encrypt with new key |

---

## Scope Transparency

The OAuth consent screen shown to users via Nylas explicitly states:

> "[App Name] is requesting permission to **read** your email. It will only access financial emails such as receipts, bank alerts, and invoices. It cannot send email, delete email, or access drafts."

This is displayed:
1. In the app UI before the OAuth redirect ("You're about to connect Gmail...")
2. On the Google/Microsoft consent screen (from the OAuth app description)
3. In the Privacy Policy

---

*See [workflows/core-workflows.md](../workflows/core-workflows.md#workflow-2) for the inbox scan pipeline flow.*
*See [security/auth-strategy.md](../security/auth-strategy.md) for OAuth token security.*
*See [architecture/system-modules.md](../architecture/system-modules.md#module-2) for module design overview.*
