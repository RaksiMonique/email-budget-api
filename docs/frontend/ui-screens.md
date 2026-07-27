# UI / UX Skeleton — L. Screen Designs and Flows

## Technology: Next.js 15 + shadcn/ui + Tailwind + React Hook Form

---

## Screen List

| Screen | Route | Priority |
|--------|-------|---------|
| Connect Inbox | `/settings/inboxes` | MVP |
| Forwarding Setup | `/settings/forwarding` | MVP |
| Import Settings | `/settings/import` | MVP |
| Import History | `/history` | MVP |
| Pending Transactions Review | `/review` | MVP |
| Transaction Review Detail | `/review/[id]` | MVP |
| Duplicate Resolution | `/review/[id]/duplicate` | MVP |
| Merchant Rules | `/settings/rules/merchants` | Phase 2 |
| Category Rules | `/settings/rules/categories` | Phase 2 |
| Privacy / Data Controls | `/settings/privacy` | MVP |

---

## Screen 1: Connect Inbox

**Route:** `/settings/inboxes`

**Purpose:** Allow users to connect, view, and manage their email inbox connections.

```
┌─────────────────────────────────────────────────────┐
│  Email Connections                     [+ Connect]  │
│                                                      │
│  ┌─────────────────────────────────────────────┐   │
│  │ ✓ Gmail (rowan@gmail.com)                   │   │
│  │   Connected May 1, 2026                     │   │
│  │   142 emails scanned  •  Last scan: 5m ago  │   │
│  │   [Scan Now]  [Disconnect]                   │   │
│  └─────────────────────────────────────────────┘   │
│                                                      │
│  Connect another inbox:                             │
│  [G Gmail]  [⊞ Outlook]                            │
│                                                      │
│  ℹ️ We only read financial emails. We never send    │
│     email on your behalf.  Learn more              │
└─────────────────────────────────────────────────────┘
```

**States:**
- Empty state: "No inboxes connected — connect Gmail or Outlook to start"
- Connecting: OAuth redirect spinner
- Error: "Connection failed — try again" with reason
- `needs_reauth`: yellow banner "Your Gmail connection needs to be re-authorized"
- Scanning indicator on "Scan Now" button

**Components:**
- `InboxConnectionCard`: shows connection details, health, action buttons
- `OAuthConnectButton`: triggers auth-url flow
- `DisconnectDialog`: confirms disconnect + asks about deleting imported emails

---

## Screen 2: Forwarding Setup

**Route:** `/settings/forwarding`

**Purpose:** Show the user's unique forwarding address and explain how to use it.

```
┌─────────────────────────────────────────────────────┐
│  Email Forwarding                                    │
│                                                      │
│  Forward any receipt or financial email to:         │
│                                                      │
│  ┌─────────────────────────────────────────────┐   │
│  │  abc12345@fintrack.raksimoni.com  [Copy]    │   │
│  └─────────────────────────────────────────────┘   │
│                                                      │
│  47 emails received via this address               │
│                                                      │
│  How to use:                                        │
│  • Forward receipts from Amazon, Apple, Uber...     │
│  • Forward bank alert emails                        │
│  • Forward invoices from any service                │
│                                                      │
│  [Regenerate Address]  (old address will stop working)│
└─────────────────────────────────────────────────────┘
```

**Components:**
- `CopyAddressButton`: copies to clipboard with toast confirmation
- `RegenerateDialog`: confirms address regeneration with warning

---

## Screen 3: Import Settings

**Route:** `/settings/import`

**Purpose:** Configure scanning behavior, retention, and auto-approve rules.

```
┌─────────────────────────────────────────────────────┐
│  Import Settings                                     │
│                                                      │
│  Scan frequency                                      │
│  ○ Real-time (as emails arrive)                     │
│  ● Every 15 minutes                                 │
│  ○ Every hour                                       │
│  ○ Manual only                                      │
│                                                      │
│  Email retention                                    │
│  Keep raw email content for: [90 days ▾]           │
│  After that, only transaction data is kept.        │
│                                                      │
│  Auto-approve                                       │
│  □ Auto-approve transactions with confidence ≥ 95%  │
│                                                      │
│  Budget app webhook                                 │
│  URL: [________________________]                    │
│  Secret: [_______________] [Regenerate]             │
│                                                      │
│  [Save Settings]                                    │
└─────────────────────────────────────────────────────┘
```

---

## Screen 4: Import History

**Route:** `/history`

**Purpose:** Browse all imported emails with filtering and status indicators.

```
┌─────────────────────────────────────────────────────┐
│  Import History          [Filter ▾]  [Search...]   │
│                                                      │
│  Status: All ▾   Source: All ▾   Date: Last 30d ▾  │
│                                                      │
│  ┌────────────────────────────────────────────────┐ │
│  │ ✓ Amazon                      $45.99  May 6    │ │
│  │   receipts@amazon.com · Merchant Receipt       │ │
│  │   Confidence: 94%  •  Approved                 │ │
│  └────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────┐ │
│  │ ⏳ Chase Bank                  $12.50  May 6   │ │
│  │   alerts@chase.com · Bank Alert                │ │
│  │   Confidence: 88%  •  Pending Review           │ │
│  └────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────┐ │
│  │ ⚠️ Unknown Sender             ???   May 5       │ │
│  │   billing@unknownco.com · Extraction Failed    │ │
│  │   [View] [Enter Manually]                      │ │
│  └────────────────────────────────────────────────┘ │
│                                                      │
│  Showing 25 of 142   [Load more]                   │
└─────────────────────────────────────────────────────┘
```

**Status Icons:** ✓ approved, ⏳ pending, ⚠️ failed, ✗ rejected, ⊘ not financial

---

## Screen 5: Pending Transactions Review

**Route:** `/review`

**Purpose:** Primary action screen — the review queue for extracted transactions.

```
┌─────────────────────────────────────────────────────┐
│  Review Queue (18)      [Bulk Approve ▾]  [Filter] │
│                                                      │
│  ┌────────────────────────────────────────────────┐ │
│  │ Amazon Marketplace                             │ │
│  │ $45.99 USD  •  May 6, 2026  •  Visa ••••1234  │ │
│  │ Shopping  •  Confidence: ████████░░ 94%        │ │
│  │                           [Approve] [Edit] [✗]  │ │
│  └────────────────────────────────────────────────┘ │
│                                                      │
│  ┌────────────────────────────────────────────────┐ │
│  │ ⚑ Possible Duplicate  Netflix                  │ │
│  │ $15.99 USD  •  May 1, 2026                     │ │
│  │ Subscriptions  •  Confidence: ██████████ 98%   │ │
│  │                           [Approve] [Review] [✗]│ │
│  └────────────────────────────────────────────────┘ │
│                                                      │
│  ┌────────────────────────────────────────────────┐ │
│  │ ⚠️ Needs Review  Chase Bank                    │ │
│  │ $1,250.00 USD  •  May 4, 2026                  │ │
│  │ Category?  •  Confidence: ████░░░░░░ 52%       │ │
│  │                          [Review Detail] [✗]   │ │
│  └────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**Bulk Approve dropdown:**
- "Approve all high confidence (≥95%)"
- "Approve all from Amazon"
- "Approve selected" (checkbox mode)

**Filter options:**
- Min confidence slider
- By merchant
- By category
- Pending only / Flagged only

---

## Screen 6: Transaction Review Detail

**Route:** `/review/[id]`

**Purpose:** Full detail view for editing and approving a single transaction.

```
┌─────────────────────────────────────────────────────┐
│  ← Back   Transaction Detail                        │
│                                                      │
│  From: receipts@amazon.com                         │
│  Subject: Your Amazon.com order #123-456           │
│  Received: May 6, 2026 9:15 AM                     │
│                                                      │
│  ─── Extracted Fields ───────────────────────────  │
│                                                      │
│  Merchant  [Amazon Marketplace     ] ✓ 97%         │
│  Amount    [$45.99                 ] ✓ 99%         │
│  Currency  [USD ▾                  ] ✓ 99%         │
│  Date      [2026-05-06             ] ✓ 95%         │
│  Card      [Visa ••••1234          ] ✓ 92%         │
│  Category  [Shopping ▾             ] ✓ rules       │
│  Type      [Debit ▾                ] ✓ 96%         │
│  Notes     [________________________]               │
│                                                      │
│  Raw snippet:                                       │
│  ┌──────────────────────────────────────────────┐  │
│  │ "Order Total: $45.99                         │  │
│  │  Payment method: Visa ending in 1234"        │  │
│  └──────────────────────────────────────────────┘  │
│                                                      │
│  [View Full Email]  [Report Parse Issue]           │
│                                                      │
│  ─────────────────────────────────────────────     │
│  [Approve Transaction]           [Reject]           │
└─────────────────────────────────────────────────────┘
```

**Editable fields:** All fields are editable inline.
**Confidence indicators:** Per-field confidence shown as color-coded percentage.
**Raw snippet:** Shows the exact text excerpt used for extraction.

---

## Screen 7: Duplicate Resolution

**Route:** `/review/[id]/duplicate`

**Purpose:** Help user resolve a suspected duplicate transaction.

```
┌─────────────────────────────────────────────────────┐
│  Possible Duplicate                                  │
│                                                      │
│  We found a similar transaction in your records.   │
│  These might be the same purchase from two emails. │
│                                                      │
│  ┌─── This Transaction ──────────────────────────┐ │
│  │ Amazon  •  $45.99  •  May 6  •  Visa ••••1234 │ │
│  │ Source: Forwarded email                        │ │
│  └────────────────────────────────────────────────┘ │
│                                                      │
│  ┌─── Already in your records ───────────────────┐ │
│  │ Amazon  •  $45.99  •  May 6  •  Visa ••••1234 │ │
│  │ Source: Gmail inbox scan                       │ │
│  │ Approved: May 5, 2026 at 11:30 AM             │ │
│  └────────────────────────────────────────────────┘ │
│                                                      │
│  Similarity: 94%  (same amount + merchant + date)  │
│                                                      │
│  [Reject This One — Keep Existing]                 │
│  [Keep Both — Different Transactions]              │
│  [Replace Existing With This One]                  │
└─────────────────────────────────────────────────────┘
```

---

## Screen 8: Merchant Rules

**Route:** `/settings/rules/merchants`

```
┌─────────────────────────────────────────────────────┐
│  Merchant Rules               [+ New Rule]          │
│                                                      │
│  Your rules run before system rules.               │
│                                                      │
│  ┌────────────────────────────────────────────────┐ │
│  │ "AMZN*"  →  Amazon  /  Shopping     [Edit][✗] │ │
│  └────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────┐ │
│  │ "UBER *"  →  Uber  /  Transport     [Edit][✗] │ │
│  └────────────────────────────────────────────────┘ │
│                                                      │
│  ─── System Rules (read-only) ────────────────     │
│  "Netflix*"  →  Netflix  /  Subscriptions          │
│  "Spotify*"  →  Spotify  /  Subscriptions          │
│  "WHOLEFDS*" →  Whole Foods  /  Groceries          │
│  [Show all system rules...]                         │
└─────────────────────────────────────────────────────┘
```

---

## Screen 9: Category Rules

**Route:** `/settings/rules/categories`

Similar layout to Merchant Rules. Allows matching on merchant name, sender domain, subject keywords, or email type → category assignment.

---

## Screen 10: Privacy / Data Controls

**Route:** `/settings/privacy`

```
┌─────────────────────────────────────────────────────┐
│  Privacy & Data Controls                            │
│                                                      │
│  Your data                                          │
│  • 1 inbox connected                               │
│  • 142 emails imported                             │
│  • 87 transactions approved                        │
│  • Storage: 2.1 MB of email content in R2          │
│  • Oldest data: February 1, 2026                   │
│                                                      │
│  [Export My Data]   (GDPR data export, sent by email)│
│                                                      │
│  Email content retention                           │
│  Raw email content is auto-deleted after 90 days. │
│  [Delete all raw email content now]                │
│                                                      │
│  Audit history                                     │
│  [View Audit Log]                                  │
│                                                      │
│  ─────────────────────────────────────────────     │
│  Danger Zone                                       │
│  [Delete All Imported Emails]                      │
│  [Delete Account and All Data]                     │
│                                                      │
│  Deleting your account removes all data within    │
│  30 days. You can cancel within 7 days.           │
└─────────────────────────────────────────────────────┘
```

---

## Frontend Architecture Notes

**Data fetching:**
- React Query (TanStack Query) for server state caching + optimistic updates
- Review queue: polling every 30 seconds OR WebSocket for real-time new transaction badge

**Key React patterns:**
- Optimistic UI for approve/reject (instant local state update, revert on error)
- Infinite scroll or pagination for import history
- Toast notifications for async actions (approve triggers toast with undo button)

**Navigation:**
- `/review` is the primary screen — accessible from app nav with badge count
- All other screens under `/settings/*`

---

*See [architecture/stack-decisions.md](../architecture/stack-decisions.md) for frontend stack rationale.*
