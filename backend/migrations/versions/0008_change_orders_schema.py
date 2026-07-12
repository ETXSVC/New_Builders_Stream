"""Change orders schema: change_orders and its RLS policy.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-11

Per docs/04-database-schema.md Section 4. This is the table Phase 1
explicitly deferred (see that plan's top-of-doc "Explicitly Out of Scope"
note) — now unblocked since `esignatures` exists as of Task 2.17 (migration
0006). `down_revision = "0007"`, after `estimates`/`estimate_line_items`,
per the corrected sequence noted in 0007's own module docstring (this
migration follows immediately after the plan's actual landed order, not any
stale literal numbering in the plan text).

`change_orders` is a plain, flat, single-table, company-scoped resource — no
hierarchy/inheritance concern of its own (unlike `cost_catalog_items`'s
bidirectional policy, 0005), and no join-based scoping either (unlike a
child table such as `estimate_line_items` which shares an `estimate_id`
lineage concept) — so it gets its OWN ordinary `tenant_isolation` policy, the
same single FOR ALL policy per table shape 0007 gave `estimates`/
`estimate_line_items`. Guarded-cast
NULLIF(current_setting('app.current_tenant', true), '')::uuid (see 0001's
long comment for why the guard is required) routed through
get_all_descendant_ids() so a parent company's session also sees its
descendant branches' rows.

`schedule_impact_days` is written here as `nullable=False,
server_default="0"` rather than the schema doc sketch's literal
`INT DEFAULT 0` (which does not say NOT NULL) — a deliberate, minor
refinement matching the ChangeOrder ORM model's own NOT NULL choice
(app/models/change_order.py), in the same spirit as 0007's own several
refinements over the schema doc's sketch (see that migration's module
docstring for the established "the schema doc is illustrative, migrations
refine it where a NOT NULL is clearly the intended behavior" precedent).

**No REVOKE here (design decision #4, same rationale as 0007) — a
deliberate contrast with 0003/0004/0006's unconditionally-immutable tables.**
`change_orders.status` transitions via the normal application-layer state
machine (Task 2.21, not yet built) — same shape as `estimates.status`
(0007), not unconditionally immutable the way `communication_logs`,
`daily_logs`/`documents`, and `esignatures` are. A blanket `REVOKE UPDATE`
would break the pending -> approved/rejected transition entirely, and a
GRANT/REVOKE can't be conditioned on a column's runtime value — Postgres
grants are not row- or state-aware. app_user retains SELECT, INSERT, UPDATE,
DELETE on this table via 0001's `ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user`, issued by the
same `postgres` owner role that runs every migration (design decision #1;
MIGRATIONS_DATABASE_URL connects as postgres), so it applies automatically
here too without any explicit GRANT in this file.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "change_orders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # ON DELETE CASCADE per the schema doc:
        # `project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE`
        # — same shape as Phase.project_id's cascade (0004).
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # No ondelete here, matching the schema doc's
        # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
        # clause) — same convention as Estimate.company_id / Project.company_id.
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("description", sa.Text, nullable=False),
        # Per US-3.6, can legitimately be positive or negative — no CHECK
        # constraint restricting sign.
        sa.Column("cost_delta", sa.Numeric(12, 2), nullable=False),
        # NOT NULL with server_default="0" — a deliberate refinement over the
        # schema doc sketch's literal `INT DEFAULT 0`; see module docstring.
        sa.Column("schedule_impact_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        # Nullable: unset until the ChangeOrder is approved, mirroring
        # `estimates.esignature_id`'s own nullable-until-approved pattern
        # (0007). FK to `esignatures(id)`, which exists as of 0006.
        sa.Column(
            "esignature_id", UUID(as_uuid=True), sa.ForeignKey("esignatures.id"), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected')",
            name="ck_change_orders_status",
        ),
    )

    # --- Row-Level Security -------------------------------------------------
    #
    # Guarded NULLIF(...)::uuid cast — see 0001's long comment for the exact
    # connection-pooling failure mode this prevents. Routed through
    # get_all_descendant_ids() (0001) so a parent company's session also sees
    # its descendant branches' rows, not just its own — the ordinary shape,
    # not cost_catalog_items' bidirectional one (0005). A single flat,
    # company-scoped `tenant_isolation` policy, same shape 0007 gave
    # `estimates`/`estimate_line_items`.
    op.execute("ALTER TABLE change_orders ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON change_orders FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- No REVOKE here (design decision #4) --------------------------------
    # `change_orders.status` is conditionally mutable via the application
    # layer (Task 2.21), same rationale 0007 gives for `estimates`/
    # `estimate_line_items` having no REVOKE. See module docstring above.


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON change_orders")
    op.drop_table("change_orders")
