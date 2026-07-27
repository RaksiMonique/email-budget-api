"""Seed merchant-normalization rules + category map (Phase 1 in-memory).

`match_type` in {starts_with, contains, exact, regex}. Rules are evaluated by
match-type priority (starts_with -> contains -> exact -> regex); within a type,
earlier entries win, so put more specific patterns first (Uber Eats before Uber).
"""
from __future__ import annotations

MERCHANT_RULES: list[dict[str, str]] = [
    {"match_type": "contains", "pattern": "AMAZON", "normalized": "Amazon", "category": "Shopping"},
    {"match_type": "contains", "pattern": "AMZN", "normalized": "Amazon", "category": "Shopping"},
    {"match_type": "contains", "pattern": "UBER EATS", "normalized": "Uber Eats", "category": "Food & Drink"},
    {"match_type": "contains", "pattern": "UBER", "normalized": "Uber", "category": "Transport"},
    {"match_type": "contains", "pattern": "NETFLIX", "normalized": "Netflix", "category": "Entertainment"},
    {"match_type": "contains", "pattern": "SPOTIFY", "normalized": "Spotify", "category": "Entertainment"},
    {"match_type": "contains", "pattern": "DOORDASH", "normalized": "DoorDash", "category": "Food & Drink"},
    {"match_type": "contains", "pattern": "STARBUCKS", "normalized": "Starbucks", "category": "Food & Drink"},
    {"match_type": "contains", "pattern": "APPLE", "normalized": "Apple", "category": "Shopping"},
]

# fallback: normalized merchant name -> category
CATEGORY_MAP: dict[str, str] = {
    "Amazon": "Shopping",
    "Netflix": "Entertainment",
    "Spotify": "Entertainment",
    "Uber": "Transport",
    "Uber Eats": "Food & Drink",
    "DoorDash": "Food & Drink",
    "Starbucks": "Food & Drink",
    "Apple": "Shopping",
}
