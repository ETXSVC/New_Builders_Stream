"""Tenant lifecycle from the platform console: create, rename, soft-delete.

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-30

Migration 0023 gave the console a tenant's *entitlements* — tier, status,
seats, per-module overrides — and deliberately nothing else. Its role holds
`SELECT` on `companies` and no write at all, so an operator could change
what a customer may do but not bring a customer into existence, correct a
name typed wrong at signup, or take a tenant out of service.

This adds those three, and the shape of what is granted matters more than
the column:

1. `companies.deleted_at` — SOFT delete. Removing a tenant for real means
   deleting rows across ~40 tables whose `company_id` foreign keys are
   almost all NO ACTION (only `company_users` and `invitations` cascade);
   `backend/scripts/prune_dev_tenants.py` needs a retry loop and the TABLE
   OWNER to do it, and that is the correct home for an irreversible
   cross-tenant delete — a shell and database access, not a web session.
   What an operator needs from a console is "stop this tenant working",
   which is reversible and is what this column expresses.

   NOT a status value on `subscriptions`. Status is Stripe's vocabulary and
   `POST /webhooks/stripe` writes it; a tenant taken out of service by an
   operator must not be revived by a routine `customer.subscription.updated`.
   `manual_status_override` exists precisely because that collision already
   happened once, and layering "deleted" into the same column would
   reintroduce it. This is a property of the COMPANY, so it lives there.

   AND NOT `companies.is_active`, which already exists and is the obvious
   candidate. That column has been on the table since migration 0001,
   defaults to true, is exposed in `CompanyResponse` — and is read by
   NOTHING. No policy, no dependency, no query gates on it. Making a dead
   flag suddenly load-bearing would change the meaning of a field already
   in an API response, and a boolean cannot answer "when", which is the
   first thing anyone asks of a tenant that stopped working.

   So `deleted_at` is authoritative and `is_active` is kept in step with it
   by the routes that write either (true when live, false when deleted).
   The alternative — a generated column, `is_active GENERATED ALWAYS AS
   (deleted_at IS NULL)` — would make disagreement impossible and is the
   better end state, but generated columns reject explicit INSERTs and
   `tests/test_cost_catalog_inheritance.py` inserts `is_active` by hand.
   Converting it is a follow-up worth doing, not a thing to smuggle in
   here.

2. The write grants the console now needs, and no more:

       INSERT on companies, users, company_users, subscriptions
       UPDATE on companies

   `UPDATE` on `companies` is what carries both the rename and the
   soft-delete/restore. There is still **no DELETE granted to
   `platform_admin` on anything** — the console cannot destroy a row in any
   table, which keeps 0023's central property intact: a bug in the console
   cannot lose a customer's data. `tests/test_platform_admin.py` asserts
   this at the catalog level rather than trusting the sentence.

   `companies.parent_id` stays immutable (migration 0021's trigger) and no
   grant here changes that: creating a tenant means creating a ROOT, and
   re-parenting an existing one remains a migration, never a write.

3. A partial index on `deleted_at`. The product path's question is always
   "is this company live", so the index covers the live rows —
   `WHERE deleted_at IS NULL` — rather than the rarer deleted ones.

WHAT IS NOT GRANTED, DELIBERATELY: `UPDATE` on `users`. Creating a tenant
writes an owner user once; changing a customer's user afterwards (email,
password, name) is account administration, not tenant administration, and
an operator able to rewrite a customer's login credential is a different
and much larger trust claim than anything else in this console.
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Set by the platform console to take a tenant out of service. "
                "NULL means live. Soft: no row is removed, and clearing it "
                "restores the tenant exactly as it was."
            ),
        ),
    )

    # Partial, because every product-path lookup asks for the live rows and
    # a full index would carry the deleted ones for no reader.
    op.create_index(
        "ix_companies_live",
        "companies",
        ["id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # --- is this company live, ancestors included? -----------------------
    #
    # SECURITY DEFINER because the request path cannot answer this itself.
    # `companies`' SELECT policy scopes rows to
    # `get_all_descendant_ids(app.current_tenant)`, which by construction
    # contains a company's DESCENDANTS and never its ancestors — so a branch
    # asking "has my parent been taken out of service?" reads zero rows and
    # would conclude "no". Deleting a parent has to take its whole subtree
    # out with it, or an operator retiring a customer leaves that customer's
    # branch offices signing in normally.
    #
    # Narrow on purpose, in the same spirit as the tenant lookup
    # `app/tasks/accounting_sync.py` uses: it takes one id, returns one
    # boolean, and exposes no row. STABLE, so it is cached within a
    # statement; the walk is total because migration 0021 makes `parent_id`
    # immutable, so the chain cannot become a cycle mid-request.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION is_company_live(target UUID)
        RETURNS BOOLEAN
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            WITH RECURSIVE ancestry AS (
                SELECT id, parent_id, deleted_at FROM companies WHERE id = target
                UNION ALL
                SELECT c.id, c.parent_id, c.deleted_at
                FROM companies c JOIN ancestry a ON c.id = a.parent_id
            )
            SELECT COALESCE(bool_and(deleted_at IS NULL), false) FROM ancestry;
        $$
        """
    )
    # COALESCE(..., false): an unknown company id yields no rows, and
    # `bool_and` over zero rows is NULL. "I have never heard of this
    # company" must not read as "live".
    op.execute("GRANT EXECUTE ON FUNCTION is_company_live(UUID) TO app_user")
    op.execute("GRANT EXECUTE ON FUNCTION is_company_live(UUID) TO platform_admin")
    op.execute("GRANT EXECUTE ON FUNCTION is_company_live(UUID) TO scanner")

    # --- what the console may now write ---------------------------------
    #
    # Four INSERTs and one UPDATE. Creating a tenant is one transaction
    # writing a company, its owner user, the membership joining them, and
    # the trial subscription — the same four rows `POST /auth/register`
    # writes, because it is the same act performed by a different actor.
    op.execute("GRANT INSERT ON companies TO platform_admin")
    op.execute("GRANT INSERT ON users TO platform_admin")
    op.execute("GRANT INSERT ON company_users TO platform_admin")
    op.execute("GRANT INSERT ON subscriptions TO platform_admin")
    # Carries the rename AND the soft-delete/restore. Note the absence of a
    # DELETE grant anywhere in this migration: that is the invariant 0023
    # established and this one preserves.
    op.execute("GRANT UPDATE ON companies TO platform_admin")


def downgrade() -> None:
    op.execute("REVOKE INSERT ON companies FROM platform_admin")
    op.execute("REVOKE INSERT ON users FROM platform_admin")
    op.execute("REVOKE INSERT ON company_users FROM platform_admin")
    op.execute("REVOKE INSERT ON subscriptions FROM platform_admin")
    op.execute("REVOKE UPDATE ON companies FROM platform_admin")

    op.execute("DROP FUNCTION IF EXISTS is_company_live(UUID)")
    op.drop_index("ix_companies_live", table_name="companies")
    op.drop_column("companies", "deleted_at")
