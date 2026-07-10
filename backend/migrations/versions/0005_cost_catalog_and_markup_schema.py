"""Cost Catalog & Markup Profile schema: markup_profiles, cost_catalog_items,
and the new get_all_ancestor_ids() function + cost_catalog_items' bidirectional
RLS policy (Phase 2 plan, New Critical Design Decision #1).

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-09

Per docs/04-database-schema.md Section 5. `markup_profiles` is a plain, flat,
per-company resource (no `parent_profile_id`, no inheritance concept — design
decision #1's closing note) and gets the ordinary `tenant_isolation` policy
shape every Phase 1 table uses: a single FOR ALL policy, guarded-cast
NULLIF(current_setting('app.current_tenant', true), '')::uuid (see 0001's long
comment for why the guard is required), gated through get_all_descendant_ids()
so a parent's session also sees its descendants' rows.

`cost_catalog_items` is different: US-4.6 requires a child branch to be able
to override an inherited catalog item, which means a child-branch session
must be able to READ its parent's (and grandparent's, etc.) catalog items —
visibility flowing *upward*, something no existing table in this codebase
needs. See the extensive comment above get_all_ancestor_ids() below and above
cost_catalog_items' policy for the full mechanism and its asymmetry.

`parent_catalog_item_id` uses ON DELETE SET NULL, not the schema doc's
unspecified default (NO ACTION/RESTRICT) — a judgment call already made and
documented on the CostCatalogItem ORM model (Task 2.1): if the ancestor item
an override chain points to is deleted, the override becomes a standalone
item rather than blocking the delete (RESTRICT) or cascading into deleting
every dependent override transitively (CASCADE, destructive/surprising here).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "markup_profiles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # No ondelete: matches the schema doc's
        # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
        # clause) — same convention as Lead.company_id / Project.company_id.
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("overhead_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("profit_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        # No created_at/updated_at: docs/04-database-schema.md Section 5's
        # `markup_profiles` table has no timestamp columns at all.
    )

    op.create_table(
        "cost_catalog_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        # ON DELETE SET NULL — see module docstring for the rationale (matches
        # the CostCatalogItem ORM model, Task 2.1).
        sa.Column(
            "parent_catalog_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("cost_catalog_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("unit", sa.String(50), nullable=False),
        sa.Column("unit_rate", sa.Numeric(12, 2), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # No created_at: docs/04-database-schema.md Section 5's
        # `cost_catalog_items` table has only updated_at, matching the
        # CostCatalogItem ORM model's UpdatedAtMixin-only mixin choice.
    )

    # --- Recursive ancestor lookup (New Critical Design Decision #1) --------
    #
    # Sibling to 0001's get_all_descendant_ids(), walking parent_id UPWARD
    # instead of downward. This is required because US-4.6 needs a
    # child-branch session to be able to READ its parent's (and
    # grandparent's, etc.) cost_catalog_items rows — visibility flowing
    # *upward* — which no existing policy in this codebase supports:
    # get_all_descendant_ids(current_tenant) only ever returns the active
    # tenant plus ITS OWN descendants, never its ancestors.
    #
    # Note: this differs from the Phase 2 plan doc's literal SQL snippet for
    # this function, which omits SECURITY DEFINER and a pinned search_path
    # and would not work as written — see below for why.
    #
    # This function queries `companies` internally to walk parent_id upward,
    # exactly like get_all_descendant_ids does to walk it downward. Without
    # SECURITY DEFINER, that internal query is subject to `companies`' own
    # existing RLS SELECT policy (0001's tenant_select), which scopes
    # visibility to the caller's own tenant PLUS DESCENDANTS ONLY (via
    # get_all_descendant_ids) — never ancestors. So an unprivileged
    # (non-SECURITY-DEFINER) version of this function's own internal SELECT
    # on companies would itself be RLS-filtered down to just the starting
    # row (an ancestor is by definition NOT one of the caller's own
    # descendants), and the recursive CTE would terminate after exactly one
    # step — it would silently return almost nothing useful instead of
    # raising an error. This is a WORSE failure mode than
    # get_all_descendant_ids' infinite-recursion risk (no crash, no stack
    # depth error — just quietly wrong/empty results), which is why this
    # task's verification empirically confirms actual ancestor rows come
    # back across a multi-level chain, not just that CREATE FUNCTION succeeds.
    #
    # Marking the function SECURITY DEFINER makes its body execute with the
    # privileges of its owner (postgres, who owns `companies` and was never
    # granted FORCE ROW LEVEL SECURITY), so the internal upward traversal
    # bypasses RLS entirely and walks the full parent_id chain regardless of
    # which rows would ordinarily be visible to the calling role. This does
    # NOT weaken tenant isolation: the OUTER RLS policy that calls this
    # function (cost_catalog_items' tenant_isolation policy, below) still
    # fully gates what app_user can ultimately see/write via
    # `... IN (SELECT id FROM get_all_ancestor_ids(...))` — only this
    # function's own internal scan is exempted, and EXECUTE on it remains
    # restricted to app_user, same as get_all_descendant_ids.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION get_all_ancestor_ids(company_uuid UUID)
        RETURNS TABLE (id UUID)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            WITH RECURSIVE ancestor_tree AS (
                SELECT c.id, c.parent_id FROM companies c WHERE c.id = company_uuid
                UNION ALL
                SELECT c.id, c.parent_id FROM companies c INNER JOIN ancestor_tree at ON c.id = at.parent_id
            )
            SELECT id FROM ancestor_tree;
        $$;
        """
    )
    # Same narrow-residual reasoning as get_all_descendant_ids (0001): since
    # this function is SECURITY DEFINER, Postgres' default "EXECUTE granted
    # to PUBLIC on new functions" would let any SQL running as app_user call
    # it directly with an arbitrary company_uuid and get back that company's
    # ancestor-chain ids, regardless of the caller's own tenant context. This
    # only leaks UUID parent/child relationships (no other columns), and
    # app_user is the backend's single trusted connection role — revoking
    # PUBLIC and granting only to app_user keeps the surface as narrow as
    # possible while the function still does its job for the RLS policy
    # below.
    op.execute("REVOKE EXECUTE ON FUNCTION get_all_ancestor_ids(UUID) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION get_all_ancestor_ids(UUID) TO app_user")

    # --- Row-Level Security --------------------------------------------------
    for table in ("markup_profiles", "cost_catalog_items"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    # markup_profiles: the ordinary tenant_isolation shape (Inherited
    # Invariant #1/#2) — plain company-scoped visibility, no inheritance.
    op.execute(
        """
        CREATE POLICY tenant_isolation ON markup_profiles FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # cost_catalog_items: the NEW bidirectional policy (New Critical Design
    # Decision #1). This is the ONLY policy in this codebase whose USING and
    # WITH CHECK clauses are asymmetric in shape, not just in the tenant id
    # they're evaluated against — read this comment before "fixing" it.
    #
    # USING (governs SELECT/UPDATE/DELETE target visibility) grants
    # visibility in BOTH directions:
    #   - DOWNWARD: company_id IN get_all_descendant_ids(caller) — the same
    #     parent-sees-its-descendants visibility every other table has.
    #   - UPWARD (new): company_id IN get_all_ancestor_ids(caller) — a
    #     child-branch session can also see its parent's (grandparent's,
    #     etc.) catalog items. This is what makes US-4.6's "child branch
    #     overrides an inherited value" possible at all: without this, a
    #     child session could never even SEE the ancestor item it wants to
    #     override.
    #
    # WITH CHECK (governs what values may be WRITTEN, i.e. what a session may
    # INSERT or UPDATE a row into) is INTENTIONALLY NOT bidirectional — it
    # only allows the DOWNWARD (descendant) set, exactly like every other
    # table. A session can only ever WRITE rows scoped to itself or a branch
    # it administers; it can never fabricate a write into an ancestor's row
    # merely because it has read visibility into that row. Without this
    # asymmetry, a child-branch session could UPDATE (or, if it could guess/
    # observe the id, overwrite) a row it does not own simply because
    # get_all_ancestor_ids granted it read access — "can read broader than
    # you can write" is the deliberate design here, per the Phase 2 plan's
    # own explicit callout, not an oversight to "fix" into symmetry.
    op.execute(
        """
        CREATE POLICY tenant_isolation ON cost_catalog_items FOR ALL
        USING (
            company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid))
            OR company_id IN (SELECT id FROM get_all_ancestor_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid))
        )
        WITH CHECK (
            company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON cost_catalog_items")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON markup_profiles")
    op.execute("DROP FUNCTION IF EXISTS get_all_ancestor_ids(UUID)")
    op.drop_table("cost_catalog_items")
    op.drop_table("markup_profiles")
