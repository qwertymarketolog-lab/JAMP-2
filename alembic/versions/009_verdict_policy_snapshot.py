"""Bind verdicts to immutable canonical policy snapshots.

Revision ID: 009_verdict_policy_snapshot
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "009_verdict_policy_snapshot"
down_revision = "008_source_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "verdicts",
        sa.Column("policy_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "verdicts",
        sa.Column("policy_snapshot_sha256", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        "ck_verdict_policy_snapshot_binding",
        "verdicts",
        "(policy_snapshot IS NULL AND policy_snapshot_sha256 IS NULL) OR "
        "(policy_snapshot IS NOT NULL AND policy_snapshot_sha256 IS NOT NULL "
        "AND jsonb_typeof(policy_snapshot) = 'object' "
        "AND policy_snapshot_sha256 ~ '^[0-9a-f]{64}$')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_snapshots = bind.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM verdicts WHERE policy_snapshot IS NOT NULL)")
    ).scalar_one()
    if has_snapshots:
        raise RuntimeError(
            "cannot downgrade 009_verdict_policy_snapshot while policy snapshots exist"
        )
    op.drop_constraint("ck_verdict_policy_snapshot_binding", "verdicts", type_="check")
    op.drop_column("verdicts", "policy_snapshot_sha256")
    op.drop_column("verdicts", "policy_snapshot")
