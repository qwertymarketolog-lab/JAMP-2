"""Create immutable versioned verdict records.

Revision ID: 002_verdict_records
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "002_verdict_records"
down_revision = "001_initial_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "verdicts",
        sa.Column("sequence_id", sa.BigInteger(), sa.Identity(), nullable=False, unique=True),
        sa.Column("verdict_id", sa.String(length=255), nullable=False),
        sa.Column("claim_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evidence_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("policy_id", sa.String(length=255), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("code_version", sa.String(length=128), nullable=False),
        sa.Column("canonicalization_version", sa.String(length=64), nullable=False),
        sa.Column("decided_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("parent_hash", sa.String(length=64), nullable=True),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "status IN ('VERIFIED', 'CONTRADICTED', 'INCONCLUSIVE', "
            "'OUT_OF_SCOPE', 'AMBIGUOUS')",
            name="ck_verdict_status",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_ids) = 'array' AND jsonb_array_length(evidence_ids) > 0",
            name="ck_verdict_evidence_ids_nonempty",
        ),
        sa.PrimaryKeyConstraint("verdict_id", name="pk_verdicts"),
    )
    op.create_index("ix_verdicts_claim_id", "verdicts", ["claim_id"])
    op.execute(
        """
        CREATE FUNCTION reject_verdict_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'verdict ledger is append-only'
                USING ERRCODE = '55000';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER verdict_immutable
        BEFORE UPDATE OR DELETE ON verdicts
        FOR EACH ROW EXECUTE FUNCTION reject_verdict_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS verdict_immutable ON verdicts")
    op.execute("DROP FUNCTION IF EXISTS reject_verdict_mutation()")
    op.drop_index("ix_verdicts_claim_id", table_name="verdicts")
    op.drop_table("verdicts")
