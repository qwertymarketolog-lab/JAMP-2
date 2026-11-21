"""Block TRUNCATE on append-only evidence and verdict ledgers.

Revision ID: 006_truncate_protection
"""

from __future__ import annotations

from alembic import op

revision = "006_truncate_protection"
down_revision = "005_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_truncate()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'TRUNCATE not allowed on append-only ledger: %',
                TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table in ("evidence", "verdicts"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_truncate
            BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION prevent_truncate();
            """
        )


def downgrade() -> None:
    for table in ("evidence", "verdicts"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    op.execute("DROP FUNCTION IF EXISTS prevent_truncate()")
