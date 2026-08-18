"""per_key_webhook_routing

Revision ID: cf8693f27f1d
Revises: 4ebc09ede792
Create Date: 2026-08-18 00:34:34.150063

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'cf8693f27f1d'
down_revision = '4ebc09ede792'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # per-key webhook routing. All nullable — existing rows stay NULL (legacy/
    # global path), so nothing that's already wired breaks.
    for table in ("aliases", "webhook_config", "webhook_outbox"):
        op.add_column(table, sa.Column("api_key_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_api_key_id", table, "api_keys", ["api_key_id"], ["id"]
        )
        op.create_index(f"ix_{table}_api_key_id", table, ["api_key_id"], unique=False)


def downgrade() -> None:
    for table in ("aliases", "webhook_config", "webhook_outbox"):
        op.drop_index(f"ix_{table}_api_key_id", table_name=table)
        op.drop_constraint(f"fk_{table}_api_key_id", table, type_="foreignkey")
        op.drop_column(table, "api_key_id")
