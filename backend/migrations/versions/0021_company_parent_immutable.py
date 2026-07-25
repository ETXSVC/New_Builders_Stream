"""Make `companies.parent_id` immutable, closing the re-parent gap in
`tenant_update`'s WITH CHECK.

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-25

Migration 0001's `tenant_update` policy reads:

    WITH CHECK (
        parent_id IS NULL
        OR parent_id IN (SELECT id FROM get_all_descendant_ids(...))
    )

The `parent_id IS NULL` branch is there for INSERT's sake — a brand-new
top-level company legitimately has no parent — but `tenant_update` is an
UPDATE policy, and on UPDATE that branch permits setting `parent_id = NULL`
on an existing row. That detaches a child branch from its parent and makes
it a new ROOT: it leaves the parent's `get_all_descendant_ids` tree, so the
parent instantly loses sight of data it owned, and as a fresh root it has
no `subscriptions` row — which both `block_if_read_only` and `tier_allows`
treat as fail-OPEN, so the detached tenant gets unlimited, unbilled access
to every gated module.

Not reachable today (no route writes `parent_id`; `PATCH /companies/{id}`
renames only), but `app/main.py`'s own comment describes a company-update
route as intended, so the gap is one plausible feature away from being live.

**Why a trigger and not a tighter WITH CHECK.** The obvious fix — drop the
`IS NULL` branch from the UPDATE policy — breaks every root company:
a root's `parent_id` IS NULL, so `NULL IN (SELECT ...)` evaluates to NULL,
the check fails, and renaming a top-level company becomes impossible. What
this actually needs to express is "parent_id may not CHANGE", which is a
statement about the old row and the new one together — and a WITH CHECK
clause cannot see the old row at all. A `BEFORE UPDATE` trigger can, so it
states the rule directly instead of approximating it.

The trigger is also strictly stronger than a policy would be: it applies to
the table owner and to `scanner` too, not just to `app_user`, so no
connection in the system can re-parent a company by accident.

A genuine re-parent — a real corporate restructuring — remains possible,
but only as a deliberate migration that disables this trigger around the
statement:

    ALTER TABLE companies DISABLE TRIGGER companies_parent_id_immutable;
    -- ... the re-parent, plus whatever data movement it implies ...
    ALTER TABLE companies ENABLE TRIGGER companies_parent_id_immutable;

which is the correct amount of friction for an operation that moves an
entire subtree between tenants.
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_company_reparent()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            -- IS DISTINCT FROM, not <>: parent_id is nullable, and `NULL <>
            -- NULL` is NULL rather than false, so a plain inequality would
            -- silently permit every root-company update to pass unchecked.
            IF NEW.parent_id IS DISTINCT FROM OLD.parent_id THEN
                RAISE EXCEPTION
                    'companies.parent_id is immutable (attempted % -> %): re-parenting '
                    'moves an entire subtree between tenants and is a migration, not a write',
                    OLD.parent_id, NEW.parent_id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER companies_parent_id_immutable
        BEFORE UPDATE ON companies
        FOR EACH ROW
        EXECUTE FUNCTION forbid_company_reparent()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS companies_parent_id_immutable ON companies")
    op.execute("DROP FUNCTION IF EXISTS forbid_company_reparent()")
