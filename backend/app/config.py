"""Application settings (pydantic-settings, reads env vars / .env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env lives at the repo root; also honor a backend-local .env if present.
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/email_budget_dev"
    internal_secret: str = ""
    email_domain: str = "fintrack.raksimoni.com"

    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "email-budget-raw"

    # encrypts the budgeting-app webhook secret at rest (see security/crypto.py)
    secret_encryption_key: str = ""
    # outbox poller (disabled in tests; tests call process_due directly)
    enable_outbox_poller: bool = True

    sentry_dsn: str = ""

    @field_validator("database_url")
    @classmethod
    def _force_asyncpg_scheme(cls, v: str) -> str:
        """Render/Heroku issue postgres:// URLs; SQLAlchemy async needs +asyncpg."""
        if v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v[len("postgres://") :]
        if v.startswith("postgresql://") and "+asyncpg" not in v:
            return "postgresql+asyncpg://" + v[len("postgresql://") :]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
