"""Create append-only evidence ledger.

Revision ID: 001_initial_ledger
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "001_initial_ledger"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidence",
        sa.Column("sequence_id", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("evidence_id", sa.String(length=255), nullable=False),
        sa.Column("claim_id", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("raw_body_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("raw_sha256", sa.String(length=64), nullable=False),
        sa.Column("captured_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parsed_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("verdict_status", sa.String(length=64), nullable=False),
        sa.Column("parent_hash", sa.String(length=64), nullable=True),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("evidence_id", name="pk_evidence"),
    )
    op.create_index("ix_evidence_claim_id", "evidence", ["claim_id"])
    op.execute(
        """
        CREATE FUNCTION reject_evidence_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'evidence ledger is append-only'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER evidence_immutable
        BEFORE UPDATE OR DELETE ON evidence
        FOR EACH ROW EXECUTE FUNCTION reject_evidence_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS evidence_immutable ON evidence")
    op.execute("DROP FUNCTION IF EXISTS reject_evidence_mutation()")
    op.drop_index("ix_evidence_claim_id", table_name="evidence")
    op.drop_table("evidence")
