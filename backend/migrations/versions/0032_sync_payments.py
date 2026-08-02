"""Let a payment be a syncable record.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-01

`integration_sync_records.entity_type` has been checked against
`('invoice','expense','bill')` since migration 0013, which is exactly the
set of things that could be synced then. Payments now sync too — a tenant's
accounting software was learning about every invoice and never learning any
of them had been paid — so the constraint widens by one value.

The constraint is worth keeping rather than dropping: it is what caught this
change needing a migration at all, instead of letting a typo'd entity_type
accumulate rows nothing would ever read.
"""
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_OLD = "entity_type IN ('invoice','expense','bill')"
_NEW = "entity_type IN ('invoice','expense','bill','payment')"
_NAME = "ck_integration_sync_records_entity_type"


def upgrade() -> None:
    op.drop_constraint(_NAME, "integration_sync_records", type_="check")
    op.create_check_constraint(_NAME, "integration_sync_records", _NEW)


def downgrade() -> None:
    # Rows for the newly-allowed type would violate the narrower constraint,
    # so they go first. Deleting them is right rather than destructive: an
    # integration_sync_records row is a record of a sync ATTEMPT, derived
    # state that the next sync rebuilds — and on a downgrade nothing can
    # sync payments any more anyway.
    op.execute("DELETE FROM integration_sync_records WHERE entity_type = 'payment'")
    op.drop_constraint(_NAME, "integration_sync_records", type_="check")
    op.create_check_constraint(_NAME, "integration_sync_records", _OLD)
