"""A client-supplied key that makes a daily log safe to send twice.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-04

A field crew member writes a daily log with no signal; the queue sends it
when the van reaches coverage. If the request arrives, the row commits, and
the RESPONSE dies on the way back — which is exactly what leaving a
coverage cell produces — the queue has no way to know it succeeded, and
retrying writes the log twice.

That is worse here than in most places, because `daily_logs` is immutable at
the DATABASE level: migration 0004 does
`REVOKE UPDATE, DELETE ON daily_logs, documents FROM app_user`, and no
router exposes an update or a delete. So the duplicate cannot be cleaned up
through the product at all — removing it needs the table owner and a shell.
An at-least-once queue against an unfixable table is not an acceptable
combination, and this column is what turns it into exactly-once.

**A body field rather than an `Idempotency-Key` header**, for the same
reason `app/services/concurrency.py` uses `expected_updated_at` instead of
`If-Match`: the Next BFF forwards a fixed header allowlist, so a custom
header is silently dropped on the hop. The header form would look correct
and duplicate every replayed log.

**Unique per COMPANY, not globally.** The value is generated on a device
this system does not control, so its uniqueness is a client's claim rather
than a guarantee; scoping the constraint to the tenant keeps one company's
collision from ever being another company's error. `NULLS DISTINCT` is
Postgres's default, which is what makes the column optional: every existing
caller — the project screen's own form, and every test — sends nothing, and
any number of rows may hold NULL.

No RLS change: `daily_logs` already has its policy from 0004, and this adds
a column to an existing table rather than a new table.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "daily_logs",
        sa.Column("client_reference", UUID(as_uuid=True), nullable=True),
    )
    # The lookup this index serves is the idempotency check itself — one
    # `WHERE company_id = ... AND client_reference = ...` per replayed
    # write — so the uniqueness constraint and the read path are the same
    # index rather than two.
    op.create_index(
        "uq_daily_logs_company_client_reference",
        "daily_logs",
        ["company_id", "client_reference"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_daily_logs_company_client_reference", table_name="daily_logs")
    op.drop_column("daily_logs", "client_reference")
