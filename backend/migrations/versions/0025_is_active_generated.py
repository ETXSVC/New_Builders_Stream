"""Make `companies.is_active` derived from `deleted_at` instead of stored.

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-30

Migration 0024 introduced `companies.deleted_at` as the authoritative
"is this tenant in service" flag, and deliberately left `is_active` alone —
a boolean that has existed since migration 0001, is exposed in
`CompanyResponse`, and until 0024 was read by nothing at all. The routes
were made to write both together so the older column stopped lying.

"Written together by convention" is exactly the kind of invariant that
holds until someone adds the third writer. This removes the possibility
rather than maintaining the discipline:

    is_active BOOLEAN GENERATED ALWAYS AS (deleted_at IS NULL) STORED

Postgres now computes it, no application code can set it, and the two
cannot disagree. `app/models/company.py` marks it `Computed(...)`, which
is what keeps SQLAlchemy from emitting it in INSERTs — a generated column
rejects an explicit value, so the ORM has to know.

WHY THIS WAS A FOLLOW-UP RATHER THAN PART OF 0024. Two test fixtures
inserted `is_active` by hand, so converting the column and fixing the
tests in the same change as introducing soft delete would have mixed "the
feature" with "the cleanup" in one diff. Both call sites are updated here.

DROP-AND-ADD, not ALTER. Postgres has no `ALTER COLUMN ... ADD GENERATED`
for stored generated columns, so the column is dropped and recreated. That
is safe here specifically because the old values carry no information the
new expression cannot reproduce: nothing but 0024's own routes ever wrote
the column, and they wrote exactly `deleted_at IS NULL`. Verified before
writing this, rather than assumed --

    SELECT count(*) FILTER (WHERE is_active <> (deleted_at IS NULL)) FROM companies

-- returned 0. If that query returns anything but 0 on a database this is
about to run against, STOP: some writer this migration does not know about
exists, and dropping the column would discard its decisions.

The column also moves to the end of the table's column order, which is
invisible to everything here (no `SELECT *` consumers; both fixtures name
their columns) but is the sort of thing worth saying out loud.
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("companies", "is_active")
    op.add_column(
        "companies",
        sa.Column(
            "is_active",
            sa.Boolean(),
            sa.Computed("deleted_at IS NULL", persisted=True),
            nullable=False,
            comment=(
                "Derived from deleted_at, never written. Kept because it is "
                "part of CompanyResponse's shape; deleted_at is the fact."
            ),
        ),
    )


def downgrade() -> None:
    """Back to a plain, writable boolean -- with the values it would have had.

    The backfill is the point: dropping a generated column and adding a
    `DEFAULT true` one would silently mark every retired tenant active
    again, so a downgrade would quietly change what the data says. The
    UPDATE puts each row back to what the expression computed.
    """
    op.drop_column("companies", "is_active")
    op.add_column(
        "companies",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE companies SET is_active = (deleted_at IS NULL)")
