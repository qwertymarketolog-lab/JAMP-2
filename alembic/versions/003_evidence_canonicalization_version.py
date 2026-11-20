"""Pin canonicalization version on every evidence record.

Revision ID: 003_evidence_canonicalization_version
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "003_evidence_canonicalization_version"
down_revision = "002_verdict_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_records = bind.execute(sa.text("SELECT count(*) FROM evidence")).scalar_one()
    if existing_records:
        raise RuntimeError(
            "Evidence ledger is non-empty; stop and prepare a forward-only hash migration "
            "before adding canonicalization_version."
        )
    op.add_column(
        "evidence",
        sa.Column(
            "canonicalization_version",
            sa.String(length=64),
            nullable=False,
            server_default="jamp-json-nfc-v1",
        ),
    )
    op.alter_column("evidence", "canonicalization_version", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    existing_records = bind.execute(sa.text("SELECT count(*) FROM evidence")).scalar_one()
    if existing_records:
        raise RuntimeError(
            "Cannot downgrade a populated evidence ledger without a hash-preserving migration."
        )
    op.drop_column("evidence", "canonicalization_version")
