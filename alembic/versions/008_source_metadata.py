"""Add source capture metadata while preserving historical v1 hashes.

Revision ID: 008_source_metadata
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "008_source_metadata"
down_revision = "007_pending_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing records are v1; the server default preserves their exact hash contract.
    op.add_column(
        "evidence",
        sa.Column("hash_schema_version", sa.String(length=8), nullable=False, server_default="v1"),
    )
    op.add_column("evidence", sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "evidence",
        sa.Column("provider_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("evidence", sa.Column("source_date", sa.String(length=16), nullable=True))
    op.add_column("verdicts", sa.Column("verification_mode", sa.String(length=16), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    has_non_v1_evidence = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM evidence "
            "WHERE hash_schema_version <> 'v1' "
            "OR fetched_at IS NOT NULL "
            "OR provider_observed_at IS NOT NULL "
            "OR source_date IS NOT NULL)"
        )
    ).scalar()
    has_verification_mode = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM verdicts "
            "WHERE verification_mode IS NOT NULL)"
        )
    ).scalar()
    if has_non_v1_evidence or has_verification_mode:
        raise RuntimeError(
            "cannot downgrade 008_source_metadata while v2 evidence or "
            "verification_mode metadata exists"
        )

    op.drop_column("verdicts", "verification_mode")
    op.drop_column("evidence", "source_date")
    op.drop_column("evidence", "provider_observed_at")
    op.drop_column("evidence", "fetched_at")
    op.drop_column("evidence", "hash_schema_version")
