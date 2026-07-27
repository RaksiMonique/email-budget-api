# Authentication Strategy

> ⚠️ **SUPERSEDED (pre-redesign).** Describes Clerk JWT + Nylas OAuth, both removed. This API now uses **API-key auth (service-to-service) + `X-Internal-Secret` on `/internal/*`**; there are no end-user accounts (users are identified by `external_user_id`). Authoritative: [PLAN.md](../../PLAN.md) Phase 3, [redesign-summary.md](../architecture/redesign-summary.md).

## Overview

Authentication uses a two-layer model:
1. **App authentication**: Clerk JWT — who is this user?
2. **Inbox OAuth**: Nylas OAuth grants — what email access has this user authorized?

These are separate credential sets that must never be conflated.

---

## App Authentication: Clerk

### Flow

```
1. User signs up / logs in via Clerk
   (email/password, Google OAuth, Apple Sign-In)

2. Clerk issues JWT (RS256 signed)

3. Client includes JWT in every API request:
   Authorization: Bearer <clerk_jwt>

4. FastAPI middleware:
   - Verifies JWT signature using Clerk JWKS endpoint
   - Extracts user_id from sub claim
   - Attaches to request context

5. All database queries filter by user_id — isolation enforced in data layer
```

### FastAPI Middleware

```python
from clerk_backend_api import authenticate_request

async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
) -> User:
    try:
        auth_state = clerk.authenticate_request(request)
        clerk_user_id = auth_state.payload["sub"]
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    user = await user_repo.get_by_clerk_id(clerk_user_id)
    if not user or user.deleted_at:
        raise HTTPException(status_code=401, detail="User not found")
    
    return user
```

### Why Clerk Over Custom JWT
- OAuth with Google/Apple is complex to implement securely
- Session management, refresh tokens, device tracking are solved problems
- Clerk's React components handle login/signup UI
- Clerk webhooks (user.created, user.deleted) drive provisioning/cleanup

---

## Inbox OAuth: Nylas

### Separation from App Auth

When a user clicks "Connect Gmail":
- This is NOT the same as logging in with Google
- This is a separate OAuth grant with scope `email.readonly`
- The user may have logged in with email/password, then separately connects Gmail
- Two different Google OAuth client IDs for two different consent purposes

### OAuth Flow

```
1. Frontend: GET /api/v1/inbox-connections/auth-url?provider=google
   ← {auth_url, state}

2. Store state in Redis: oauth_state:{state} = {user_id, provider, expires: +10min}

3. Redirect user to auth_url (Nylas-hosted OAuth proxy for Google)

4. User grants email.readonly permission to the app

5. Google → Nylas → callback: GET /api/v1/inbox-connections/callback?code=X&state=Y

6. Validate state parameter:
   - Retrieve from Redis
   - Confirm user_id matches authenticated user
   - Check not expired
   - Delete from Redis (one-time use)

7. Nylas exchanges code for grant_id + access_token

8. Encrypt access_token, store InboxConnection

9. Redirect to frontend: /inbox?connection_id=uuid&status=success
```

### Token Storage

```python
# Encryption: AES-256-GCM with user-derived key
def encrypt_token(token: str, user_id: UUID) -> tuple[str, str]:
    user_key = kms.derive_user_key(user_id)
    iv = os.urandom(12)
    cipher = AES.new(user_key, AES.MODE_GCM, nonce=iv)
    ciphertext, tag = cipher.encrypt_and_digest(token.encode())
    encrypted = base64.b64encode(ciphertext + tag).decode()
    iv_b64 = base64.b64encode(iv).decode()
    return encrypted, iv_b64
```

Stored in: `inbox_connections.encrypted_access_token`, `inbox_connections.token_iv`

Never logged, never in Sentry context, never in error messages.

---

## Webhook Authentication

### Postmark Inbound Webhook
```python
# Header: X-Postmark-Inbound-Token
POSTMARK_INBOUND_TOKEN = settings.POSTMARK_INBOUND_TOKEN

def verify_postmark_webhook(request: Request) -> bool:
    token = request.headers.get("X-Postmark-Inbound-Token")
    return secrets.compare_digest(token or "", POSTMARK_INBOUND_TOKEN)
```

### Nylas Webhook
```python
# Header: X-Nylas-Signature (HMAC-SHA256)
def verify_nylas_webhook(request: Request, body: bytes) -> bool:
    signature = request.headers.get("X-Nylas-Signature")
    expected = hmac.new(
        settings.NYLAS_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    return secrets.compare_digest(signature or "", expected)
```

### Outbound Webhooks to Budget App
Budget app registers a webhook URL + secret. When delivering:
```python
def sign_webhook_payload(payload: dict, secret: str) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()

# Deliver with signature header:
# X-Budget-Signature: sha256=<hmac_hex>
# X-Budget-Timestamp: <unix_timestamp>
```

---

## Authorization: Row-Level Security

Every repository method enforces user ownership:

```python
async def get_imported_email(email_id: UUID, user_id: UUID) -> ImportedEmail:
    email = await db.get(ImportedEmail, email_id)
    if email is None:
        raise NotFoundError("Email not found")
    if email.user_id != user_id:
        raise ForbiddenError("Not your email")  # 403, not 404 (don't leak existence)
    return email
```

**Note on 403 vs 404:** For resources that shouldn't be enumerable (email IDs), return 403 — not 404 — when the resource exists but belongs to another user. Returning 404 would allow enumeration attacks.

---

## CSRF Protection

- OAuth state parameter: cryptographic random, stored in Redis, single-use, 10-minute TTL
- SameSite=Strict cookies if using cookie-based sessions (Clerk uses token-based, so less relevant)
- State validated before any OAuth token exchange

---

## Rate Limiting

```python
# Per-user API rate limiting
async def check_rate_limit(user_id: UUID, endpoint: str) -> None:
    key = f"rate_limit:api:{user_id}:{endpoint}"
    count = redis.incr(key)
    if count == 1:
        redis.expire(key, 60)  # 1-minute window
    if count > RATE_LIMITS.get(endpoint, 100):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
```

Limits:
- Standard API endpoints: 100/min per user
- Export endpoint: 2/hour per user
- Delete account: 1/day per user
- Webhook delivery (inbound): 500/min global

---

*See [security/privacy-compliance.md](privacy-compliance.md) for data handling security.*
