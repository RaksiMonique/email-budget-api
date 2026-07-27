# Merchant / Category Rules Engine

> ⚠️ **PARTIALLY SUPERSEDED (pre-redesign).** The normalization + category-suggestion *logic* is current, but the **Redis** rule cache shown here is replaced by an **in-process dict cache (10-min TTL, invalidated on mutation)** — there is no Redis in MVP. See [PLAN.md](../../PLAN.md) Phase 1 (`rules_engine.py`).

## Overview

The rules engine enriches extracted transactions with normalized merchant names and category suggestions. It runs after extraction and before the pending transaction is created. It operates in priority layers: user-defined rules win over system rules, and system rules win over AI guesses.

---

## Why a Rules Engine Matters

Raw extraction often produces merchant strings like:
- `AMZN MKTP US*2X3K9F` — should be "Amazon"
- `SQ *BLUE BOTTLE COFFE` — should be "Blue Bottle Coffee"
- `UBER* TRIP` — should be "Uber"
- `NETFLIX.COM` — should be "Netflix"

Without normalization:
- Duplicate detection fails (can't match "Amazon" to "AMZN MKTP US")
- Category suggestions are unreliable
- The review UI shows confusing raw strings

The rules engine solves all three by normalizing merchant names before they reach the user.

---

## Rule Types

### 1. Merchant Alias Rules

**Purpose:** Map a raw merchant string → normalized display name + category.

**Match types:**
| Type | Example Pattern | Matches |
|------|----------------|---------|
| `starts_with` | `AMZN` | "AMZN MKTP US*2X3K9F", "AMZN*DIGITAL" |
| `contains` | `STARBUCKS` | "STARBUCKS #12345", "STARBUCKS COFFEE" |
| `exact` | `NETFLIX.COM` | "NETFLIX.COM" only |
| `regex` | `^SQ \*(.+)` | Any Square merchant: extracts merchant name from capture group |
| `domain` | `uber.com` | Applied when sender_domain matches (classification-time) |

**Schema:**
```python
class MerchantRule:
    user_id: Optional[UUID]   # None = system rule
    match_type: str           # starts_with, contains, exact, regex
    pattern: str              # the pattern to match against
    normalized_name: str      # what to call the merchant
    category: Optional[str]   # optional category assignment
    priority: int             # lower number = higher priority
    is_system: bool
```

### 2. Category Rules

**Purpose:** Assign a spending category based on merchant, sender domain, email type, or subject keywords.

**Match fields:**
| Field | Example |
|-------|---------|
| `merchant` (normalized) | `merchant LIKE 'Netflix'` → subscriptions |
| `sender_domain` | `sender_domain = 'uber.com'` → transport |
| `email_type` | `email_type = 'subscription_invoice'` → subscriptions |
| `subject` | `subject CONTAINS 'fuel'` → transport |

---

## Execution Order

```
1. User merchant rules       (priority 1–99)
   ↓ if no match
2. System merchant rules     (priority 100–199)
   ↓ if no match
3. User category rules       (priority 1–99)
   ↓ if no match
4. System category rules     (priority 100–199)
   ↓ if no match
5. Email type → category map (hardcoded fallback)
   ↓ if no match
6. AI category suggestion    (Claude Haiku, cheap)
   ↓ if no match
7. category = "other"
```

The first rule that matches at any level wins. Processing stops.

---

## Rule Execution

```python
class RulesEngine:
    
    def __init__(self, user_id: UUID):
        self.user_id = user_id
        self._rules_cache: Optional[CompiledRules] = None
    
    async def enrich(self, extraction: ExtractionResult) -> EnrichedResult:
        rules = await self._get_compiled_rules()
        
        # Step 1: Normalize merchant name
        normalized_merchant = self._apply_merchant_rules(
            raw_merchant=extraction.merchant,
            sender_domain=extract_domain(extraction.sender_address),
            rules=rules,
        )
        
        # Step 2: Assign category
        category, category_confidence, category_source = self._apply_category_rules(
            merchant=normalized_merchant,
            sender_domain=extract_domain(extraction.sender_address),
            email_type=extraction.email_type,
            subject=extraction.subject,
            rules=rules,
        )
        
        # Step 3: AI fallback if still unresolved
        if category is None:
            category, category_confidence = await self._ai_category_fallback(
                merchant=normalized_merchant,
                email_type=extraction.email_type,
                subject=extraction.subject,
            )
            category_source = "ai"
        
        return EnrichedResult(
            merchant_normalized=normalized_merchant,
            category_suggestion=category or "other",
            category_confidence=category_confidence or 0.0,
            category_source=category_source,
        )
    
    def _apply_merchant_rules(
        self, raw_merchant: str, sender_domain: str, rules: CompiledRules
    ) -> str:
        if not raw_merchant:
            return raw_merchant
        
        merchant_upper = raw_merchant.upper()
        
        for rule in rules.merchant_rules:  # already sorted by priority
            match = self._matches(rule, merchant_upper, sender_domain)
            if match:
                # Regex rules can extract the normalized name from a capture group
                if rule.match_type == "regex" and match.group(1):
                    return match.group(1).title()
                return rule.normalized_name
        
        # No rule matched — apply generic cleanup
        return self._generic_merchant_cleanup(raw_merchant)
    
    def _generic_merchant_cleanup(self, raw: str) -> str:
        """Remove transaction IDs, asterisks, store numbers from merchant strings."""
        cleaned = raw.strip()
        # Remove trailing transaction ID patterns: *ABC123, #12345
        cleaned = re.sub(r'\s*[\*#]\s*[A-Z0-9]{4,}\s*$', '', cleaned, flags=re.IGNORECASE)
        # Remove store/location numbers: "Starbucks #4521" → "Starbucks"
        cleaned = re.sub(r'\s+#\d{3,}\s*$', '', cleaned)
        # Title case
        cleaned = cleaned.strip().title()
        return cleaned
    
    def _apply_category_rules(
        self,
        merchant: str,
        sender_domain: str,
        email_type: str,
        subject: str,
        rules: CompiledRules,
    ) -> tuple[Optional[str], Optional[float], Optional[str]]:
        for rule in rules.category_rules:
            value = {
                "merchant": merchant,
                "sender": sender_domain,
                "email_type": email_type,
                "subject": subject,
            }.get(rule.match_field, "")
            
            if self._text_matches(rule.match_type, rule.pattern, value):
                return (
                    rule.category,
                    0.95 if rule.user_id else 0.85,  # user rules = higher confidence
                    "user_rule" if rule.user_id else "system_rule",
                )
        
        # Email type fallback
        fallback = EMAIL_TYPE_CATEGORY_MAP.get(email_type)
        if fallback:
            return fallback, 0.70, "email_type_fallback"
        
        return None, None, None
    
    async def _ai_category_fallback(
        self, merchant: str, email_type: str, subject: str
    ) -> tuple[str, float]:
        # Check cache first
        cache_key = f"category_ai:{hash(f'{merchant}:{email_type}')}"
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)
        
        response = await claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system="Classify the spending category. Reply with JSON only: "
                   '{"category": "food_and_dining|groceries|transport|entertainment|'
                   'shopping|utilities|subscriptions|health|travel|atm_cash|transfers|other", '
                   '"confidence": 0.0-1.0}',
            messages=[{
                "role": "user",
                "content": f"Merchant: {merchant}\nEmail type: {email_type}\nSubject: {subject}"
            }]
        )
        
        result = json.loads(response.content[0].text)
        category = result.get("category", "other")
        confidence = float(result.get("confidence", 0.5))
        
        # Cache for 24 hours
        await redis.setex(cache_key, 86400, json.dumps([category, confidence]))
        return category, confidence
```

---

## Email Type → Category Fallback Map

```python
EMAIL_TYPE_CATEGORY_MAP = {
    "subscription_invoice":    ("subscriptions", 0.80),
    "atm_withdrawal":          ("atm_cash",      0.95),
    "transfer_notification":   ("transfers",     0.95),
    "refund_notification":     ("shopping",      0.60),  # refunds are often shopping
    "bank_alert":              (None, None),              # too ambiguous
    "credit_card_alert":       (None, None),
    "merchant_receipt":        (None, None),
    "payment_confirmation":    (None, None),
}
```

---

## System Rules — Seed Data

Seeded at startup (not in migrations — they can be updated without schema changes):

```python
SYSTEM_MERCHANT_RULES = [
    # E-commerce
    MerchantRule(pattern="AMZN", match_type="starts_with", normalized_name="Amazon", category="shopping", priority=100),
    MerchantRule(pattern="AMAZON", match_type="starts_with", normalized_name="Amazon", category="shopping", priority=101),
    MerchantRule(pattern="EBAY", match_type="starts_with", normalized_name="eBay", category="shopping", priority=110),
    MerchantRule(pattern="ETSY", match_type="starts_with", normalized_name="Etsy", category="shopping", priority=111),
    MerchantRule(pattern="SHOPIFY", match_type="contains", normalized_name=None, category="shopping", priority=120),
    
    # Ride sharing
    MerchantRule(pattern="UBER", match_type="starts_with", normalized_name="Uber", category="transport", priority=100),
    MerchantRule(pattern="LYFT", match_type="starts_with", normalized_name="Lyft", category="transport", priority=101),
    
    # Food delivery
    MerchantRule(pattern="DOORDASH", match_type="contains", normalized_name="DoorDash", category="food_and_dining", priority=100),
    MerchantRule(pattern="GRUBHUB", match_type="contains", normalized_name="GrubHub", category="food_and_dining", priority=101),
    MerchantRule(pattern="UBEREATS", match_type="contains", normalized_name="Uber Eats", category="food_and_dining", priority=102),
    MerchantRule(pattern="INSTACART", match_type="contains", normalized_name="Instacart", category="groceries", priority=110),
    
    # Streaming / Subscriptions
    MerchantRule(pattern="NETFLIX", match_type="starts_with", normalized_name="Netflix", category="subscriptions", priority=100),
    MerchantRule(pattern="SPOTIFY", match_type="starts_with", normalized_name="Spotify", category="subscriptions", priority=101),
    MerchantRule(pattern="APPLE.COM/BILL", match_type="contains", normalized_name="Apple Subscriptions", category="subscriptions", priority=102),
    MerchantRule(pattern="GOOGLE ONE", match_type="contains", normalized_name="Google One", category="subscriptions", priority=103),
    MerchantRule(pattern="DISNEY PLUS", match_type="contains", normalized_name="Disney+", category="subscriptions", priority=104),
    MerchantRule(pattern="HULU", match_type="starts_with", normalized_name="Hulu", category="subscriptions", priority=105),
    
    # Square merchants (extract name from regex)
    MerchantRule(pattern=r"^SQ \*(.{2,30})$", match_type="regex", normalized_name=None, category=None, priority=50),
    
    # Groceries
    MerchantRule(pattern="WHOLEFDS", match_type="starts_with", normalized_name="Whole Foods", category="groceries", priority=100),
    MerchantRule(pattern="WHOLE FOODS", match_type="starts_with", normalized_name="Whole Foods", category="groceries", priority=101),
    MerchantRule(pattern="TRADER JOE", match_type="starts_with", normalized_name="Trader Joe's", category="groceries", priority=102),
    MerchantRule(pattern="KROGER", match_type="starts_with", normalized_name="Kroger", category="groceries", priority=103),
    MerchantRule(pattern="WALMART", match_type="starts_with", normalized_name="Walmart", category="shopping", priority=110),  # shopping not groceries
    
    # Coffee
    MerchantRule(pattern="STARBUCKS", match_type="starts_with", normalized_name="Starbucks", category="food_and_dining", priority=100),
    MerchantRule(pattern="DUNKIN", match_type="starts_with", normalized_name="Dunkin'", category="food_and_dining", priority=101),
    
    # Fuel
    MerchantRule(pattern="SHELL", match_type="starts_with", normalized_name="Shell", category="transport", priority=100),
    MerchantRule(pattern="CHEVRON", match_type="starts_with", normalized_name="Chevron", category="transport", priority=101),
    MerchantRule(pattern="BP", match_type="exact", normalized_name="BP", category="transport", priority=102),
    MerchantRule(pattern="EXXON", match_type="starts_with", normalized_name="ExxonMobil", category="transport", priority=103),
    
    # Payments
    MerchantRule(pattern="PAYPAL", match_type="starts_with", normalized_name="PayPal", category=None, priority=100),  # category depends on purpose
    MerchantRule(pattern="VENMO", match_type="starts_with", normalized_name="Venmo", category="transfers", priority=100),
    MerchantRule(pattern="CASH APP", match_type="contains", normalized_name="Cash App", category="transfers", priority=101),
    MerchantRule(pattern="ZELLE", match_type="starts_with", normalized_name="Zelle", category="transfers", priority=102),
    
    # Technology / SaaS
    MerchantRule(pattern="GITHUB", match_type="starts_with", normalized_name="GitHub", category="subscriptions", priority=100),
    MerchantRule(pattern="DIGITALOCEAN", match_type="starts_with", normalized_name="DigitalOcean", category="subscriptions", priority=101),
    MerchantRule(pattern="OPENAI", match_type="starts_with", normalized_name="OpenAI", category="subscriptions", priority=102),
    MerchantRule(pattern="VERCEL", match_type="starts_with", normalized_name="Vercel", category="subscriptions", priority=103),
]
```

---

## Rule Compilation and Caching

Rules are compiled to regex objects at load time and cached in Redis:

```python
@dataclass
class CompiledMerchantRule:
    compiled_pattern: re.Pattern
    normalized_name: Optional[str]
    category: Optional[str]
    match_type: str
    user_id: Optional[UUID]

def compile_rules(raw_rules: list[MerchantRule]) -> list[CompiledMerchantRule]:
    compiled = []
    for rule in raw_rules:
        if rule.match_type == "regex":
            pattern = re.compile(rule.pattern, re.IGNORECASE)
        elif rule.match_type == "starts_with":
            pattern = re.compile(f"^{re.escape(rule.pattern)}", re.IGNORECASE)
        elif rule.match_type == "contains":
            pattern = re.compile(re.escape(rule.pattern), re.IGNORECASE)
        elif rule.match_type == "exact":
            pattern = re.compile(f"^{re.escape(rule.pattern)}$", re.IGNORECASE)
        else:
            continue
        compiled.append(CompiledMerchantRule(
            compiled_pattern=pattern,
            normalized_name=rule.normalized_name,
            category=rule.category,
            match_type=rule.match_type,
            user_id=rule.user_id,
        ))
    return compiled
```

Cache key: `merchant_rules:{user_id}` — TTL 10 minutes. Invalidated when user creates/updates/deletes a rule.

---

## User Rule Management

Users can add their own rules via the Merchant Rules and Category Rules screens. Their rules are injected at the top of the cascade with `priority < 100`.

**Validation on create:**
- Pattern cannot be empty
- Regex patterns are validated before storage (`re.compile(pattern)`)
- Category must be from the controlled vocabulary
- Maximum 200 rules per user

**Rule learning from corrections:**
When a user approves a transaction and **changes the category**, the system optionally offers:

> "Always use 'Groceries' for Trader Joe's? [Create rule]"

If accepted, creates a user category rule automatically.

---

## Performance

- Rules are compiled once and cached; matching is pure regex — sub-millisecond per email
- AI category fallback adds ~300ms when triggered; cached for 24h per (merchant, email_type) combination
- At 10,000 emails/day: rules handle ~95% without AI, AI handles ~500 calls/day at ~$0.0001 each = ~$0.05/day

---

*See [ai-processing/extraction-strategy.md](../ai-processing/extraction-strategy.md) for how rules run after extraction.*
*See [frontend/ui-screens.md](../frontend/ui-screens.md) for the Merchant Rules and Category Rules screens.*
*See [database/entity-schema.md](../database/entity-schema.md) for `merchant_rules` and `category_rules` table definitions.*
