"""Give the `client` role row-level scoping, and bind a signature to a real account.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-25

Until now `client` was a tenant-wide reader. RLS is company-scoped, and the
client-facing routes narrowed by *status* only — `list_estimates` filtered
`status='sent'`, `list_change_orders` filtered `status='pending'`,
`list_invoices` filtered `status != 'draft'` — never by identity. A company
with two customers therefore leaked each customer's pricing, margins,
invoices and signed contracts to the other, and `POST
/estimates/{id}/approve` (gated on `require_role("client")` plus the status
check and nothing else) let either of them legally e-sign the other's
contract.

The root cause was schema-level: nothing in the database said *which*
client a Project, Estimate or Invoice belonged to. This migration adds that
missing edge as a membership table rather than a denormalized FK on each
record:

  * one row grants one user access to one project — two homeowners on the
    same job is a row, not a schema change;
  * there is exactly one place to write the linkage, so it cannot drift
    (a `client_user_id` copied onto Estimate, Invoice, Project and
    Esignature would re-open the `company_id`-stamping bug class this
    codebase has already had to fix seven times);
  * revoking access is a DELETE, with no history rewritten.

`lead_clients` is the same table for the pre-project stage: an Estimate may
hang off a bare Lead (`estimates.project_id` is nullable), and those
estimates are exactly the ones a prospective customer is asked to sign, so
they need the same edge or the client flow breaks for new business.

Both tables are tenant-owned and carry `company_id` with its own index and
`tenant_isolation` policy — the two catalog-driven gates
(`test_company_id_index_coverage.py`, `test_rls_policy_coverage.py`) fail
without them.

`esignatures.signed_by_user_id` closes the other half of the finding. The
signature block captured `signer_name`/`signer_email` as free-text form
fields and never compared them to the authenticated caller, so the
attribution on a document whose entire purpose is legal evidence of
contract acceptance was whatever the caller typed. The column is nullable
because rows signed before this migration have no account to point at —
backfilling a guess would be worse than an honest NULL on exactly the
records where the evidence is weakest.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

# Identical DDL shape for both tables; only the parent differs. Written as
# a loop over (table, parent_table, parent_column) rather than twice by
# hand so the two can't drift in a later edit.
_MEMBERSHIP_TABLES = (
    ("project_clients", "projects", "project_id"),
    ("lead_clients", "leads", "lead_id"),
)


def upgrade() -> None:
    for table, parent_table, parent_column in _MEMBERSHIP_TABLES:
        op.create_table(
            table,
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            # No ondelete, matching every other company_id FK in this schema.
            sa.Column(
                "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
            ),
            # ondelete CASCADE on both of these, unlike company_id: a
            # membership row is meaningless once its project/lead or its
            # user is gone, and leaving orphans would hand access decisions
            # to dangling ids.
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                parent_column,
                UUID(as_uuid=True),
                sa.ForeignKey(f"{parent_table}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            # Grant-once semantics: re-granting is a 409, not a duplicate row
            # that a later revoke would only half-remove.
            sa.UniqueConstraint(parent_column, "user_id", name=f"uq_{table}_parent_user"),
        )
        op.create_index(f"ix_{table}_company_id", table, ["company_id"])
        # The scoping subquery runs `WHERE user_id = <caller>` on every
        # client-facing read; without this it is a sequential scan on a
        # table that grows with (projects x clients).
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])

        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table} FOR ALL
            USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            """
        )

    op.add_column(
        "esignatures",
        sa.Column(
            "signed_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )
    # Clients read their own signatures by this column (see
    # app/routers/esignatures.py) — the one lookup on this table that isn't
    # by primary key.
    op.create_index("ix_esignatures_signed_by_user_id", "esignatures", ["signed_by_user_id"])


def downgrade() -> None:
    op.drop_index("ix_esignatures_signed_by_user_id", table_name="esignatures")
    op.drop_column("esignatures", "signed_by_user_id")

    for table, _parent_table, _parent_column in reversed(_MEMBERSHIP_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_index(f"ix_{table}_user_id", table_name=table)
        op.drop_index(f"ix_{table}_company_id", table_name=table)
        op.drop_table(table)
