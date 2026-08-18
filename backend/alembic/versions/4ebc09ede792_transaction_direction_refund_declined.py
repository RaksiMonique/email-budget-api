"""transaction_direction_refund_declined

Revision ID: 4ebc09ede792
Revises: d34a3504ce8d
Create Date: 2026-08-18 00:07:50.818484

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '4ebc09ede792'
down_revision = 'd34a3504ce8d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default backfills existing rows (all treated as debit charges, the
    # prior behavior); non-null going forward.
    op.add_column(
        "extraction_results",
        sa.Column("direction", sa.String(length=8), server_default="debit", nullable=False),
    )
    op.add_column(
        "extraction_results",
        sa.Column(
            "is_probable_refund", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column(
        "extraction_results",
        sa.Column("is_declined", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("extraction_results", "is_declined")
    op.drop_column("extraction_results", "is_probable_refund")
    op.drop_column("extraction_results", "direction")
