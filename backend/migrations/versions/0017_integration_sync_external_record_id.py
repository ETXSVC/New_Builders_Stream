"""Integration sync records: track the external provider's own record id.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-23

Closes a gap in Task 4.12's sync actor (`app/tasks/accounting_sync.py`):
`push_invoice`/`push_expense`/`push_bill` return the provider's own
external record id, but nothing persisted it — there was no durable trace
of which external record a given Invoice/Expense/Bill actually produced,
and no basis for the accompanying idempotency-key fix (same task) to prove
a push had already completed. Nullable because existing rows (and any
future row created before a first successful push) have no external id
yet — `status='pending'`/`'failed'` rows never had one to record.
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integration_sync_records",
        sa.Column("external_record_id", sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("integration_sync_records", "external_record_id")
