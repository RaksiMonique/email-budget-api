"""Mint an API key for the budgeting app: prints the key ONCE, stores its hash.

    python -m app.seed.create_api_key "budgeting-app"
"""
from __future__ import annotations

import asyncio
import secrets
import sys

from app.db.session import async_session
from app.models import ApiKey
from app.security.api_key import hash_key


async def main(label: str) -> None:
    raw = f"eb_{secrets.token_urlsafe(32)}"
    async with async_session() as db:
        db.add(ApiKey(key_hash=hash_key(raw), label=label))
        await db.commit()
    print(f"API key created (label={label!r}) — shown ONCE, store it now:")
    print(raw)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "default"))
