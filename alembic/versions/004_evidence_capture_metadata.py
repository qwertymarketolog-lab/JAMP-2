"""Store source timestamps and capture/parser versions on evidence.

Revision ID: 004_evidence_capture_metadata
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "004_evidence_capture_metadata"
down_revision = "003_evidence_canon_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing_records = bind.execute(sa.text("SELECT count(*) FROM evidence")).scalar_one()
    if existing_records:
        raise RuntimeError(
            "Evidence ledger is non-empty; stop and prepare a forward-only metadata migration."
        )
    op.add_column(
        "evidence",
        sa.Column("source_timestamp", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "evidence",
        sa.Column(
            "body_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "evidence",
        sa.Column(
            "provider_adapter_version",
            sa.String(length=64),
            nullable=False,
            server_default="provider-adapter-v1",
        ),
    )
    op.add_column(
        "evidence",
        sa.Column(
            "parser_version",
            sa.String(length=64),
            nullable=False,
            server_default="provider-parser-v1",
        ),
    )
    op.alter_column("evidence", "body_complete", server_default=None)
    op.alter_column("evidence", "provider_adapter_version", server_default=None)
    op.alter_column("evidence", "parser_version", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    existing_records = bind.execute(sa.text("SELECT count(*) FROM evidence")).scalar_one()
    if existing_records:
        raise RuntimeError(
            "Cannot downgrade populated evidence records without a hash-preserving migration."
        )
    op.drop_column("evidence", "parser_version")
    op.drop_column("evidence", "provider_adapter_version")
    op.drop_column("evidence", "body_complete")
    op.drop_column("evidence", "source_timestamp")
