"""Add expiring ownership leases to idempotency reservations.

Revision ID: 007_pending_lease
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "007_pending_lease"
down_revision = "006_truncate_protection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "idempotency_records",
        sa.Column("owner_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "idempotency_records",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "idempotency_records",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    # Legacy PENDING reservations are immediately eligible for atomic takeover.
    op.execute(
        """
        UPDATE idempotency_records
        SET lease_expires_at = created_at_utc - INTERVAL '1 second',
            owner_token = key_hash
        WHERE status = 'PENDING'
        """
    )


def downgrade() -> None:
    op.drop_column("idempotency_records", "attempt_count")
    op.drop_column("idempotency_records", "lease_expires_at")
    op.drop_column("idempotency_records", "owner_token")
