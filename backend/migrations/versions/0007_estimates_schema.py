"""Estimates schema: estimates, estimate_line_items, and their RLS policies.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-09

Per docs/04-database-schema.md Section 5, plus design decision #5's three
PDF-tracking columns on `estimates` (not in the schema doc — a documented,
deliberate extension, same category as Phase 1's `PATCH /projects/{id}`
decision: a necessary extension beyond the documented schema, not a
deviation from intent).

**Re-sequencing note**: Task 2.8's own spec text literally says
`down_revision = "0005"` in one place, but that is the plan document's
original (superseded) numbering, written before the esignatures-reordering
correction was finalized. `estimates.esignature_id` is a hard FK to
`esignatures(id)` (already added to the ORM model in Task 2.7), and Postgres
requires the referenced table to exist at CREATE TABLE time — so
`esignatures` had to land in an EARLIER migration than this one. The plan
doc's own Task 2.8 section resolves this explicitly: "Re-sequence:
`esignatures` becomes migration `0006`, `estimates`/`estimate_line_items`
becomes `0007`... Follow that order, not this task's own literal number in
isolation." `0006_esignatures_schema.py` (Task 2.17, already landed) is
therefore this migration's actual parent — hence `down_revision = "0006"`
here, not `"0005"`.

Both `estimates` and `estimate_line_items` are plain, flat, company-scoped
tables — no hierarchy/inheritance concern of their own (unlike
`cost_catalog_items`' bidirectional policy, 0005) — so each gets its OWN
separate ordinary `tenant_isolation` policy, the same two-independent-
policies shape 0005 gave `markup_profiles`/`cost_catalog_items`: a single
FOR ALL policy per table, guarded-cast
NULLIF(current_setting('app.current_tenant', true), '')::uuid (see 0001's
long comment for why the guard is required), gated through
get_all_descendant_ids() so a parent company's session also sees its
descendant branches' rows. `estimate_line_items` is independently RLS-scoped
by its OWN `company_id` column (not inherited through a join to
`estimates`), matching how `cost_catalog_items` and `markup_profiles` each
carry and enforce their own `company_id` rather than relying on a parent
table's policy to gate child rows.

`estimates` is created before `estimate_line_items` in this file:
`estimate_line_items.estimate_id` is a NOT NULL FK with `ON DELETE CASCADE`
to `estimates(id)`, and Postgres processes each `op.create_table` in
sequence, so the parent table must already exist before the child's FK can
be declared — same parent-before-child ordering as `projects` before
`phases` in 0004.

**No REVOKE on either table (design decision #4) — a deliberate contrast
with 0003/0004/0006, not an oversight.** Those three migrations each
unconditionally REVOKEd UPDATE/DELETE at the grant level for tables that are
*always* immutable (`communication_logs`, `daily_logs`/`documents`,
`esignatures`). `estimates`/`estimate_line_items` are different in kind:
they're mutable while `estimates.is_snapshotted = false` (a PM actively
building an Estimate needs to add/edit/remove line items and recalculate
freely) and become immutable only once `is_snapshotted` flips to `true` on
approval. A blanket `REVOKE UPDATE` would break the normal editing workflow
entirely, and a GRANT/REVOKE can't be conditioned on another column's
runtime value — Postgres grants are not row- or state-aware. This
conditional immutability is therefore enforced entirely at the APPLICATION
layer in later tasks: `PUT /estimates/{id}/lines` (Task 2.11) and
`POST /estimates/{id}/calculate` (Task 2.12) both check
`estimate.is_snapshotted` first and return `409 Conflict` if true, before
touching any row. A future reader seeing `estimates`/`estimate_line_items`
with no REVOKE immediately after three migrations in a row that DID add one
should not "fix" this as a gap — it is the correct, documented shape for a
conditionally-immutable table.

Verified empirically against the live Postgres container (see task notes)
that app_user already has SELECT, INSERT, UPDATE, DELETE on both new tables
without any explicit GRANT here — migrations run as the `postgres` owner
role (design decision #1; MIGRATIONS_DATABASE_URL connects as postgres),
and 0001's `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT,
UPDATE, DELETE ON TABLES TO app_user` was issued by that same role, so it
applies automatically to every table `postgres` creates afterward in this
schema, including these two.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "estimates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # No ondelete: matches the schema doc's
        # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
        # clause) — same convention as Project.company_id / MarkupProfile.company_id.
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        # Nullable: an Estimate can be created against a bare Lead with no
        # Project yet (US-4.1: "against a Lead or Project"). No ondelete,
        # matching the schema doc's `project_id UUID REFERENCES projects(id)`.
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True),
        # Nullable for the mirror-image reason — an Estimate against an
        # already-drafted Project has no lead_id. No ondelete, matching the
        # schema doc's `lead_id UUID REFERENCES leads(id)`.
        sa.Column("lead_id", UUID(as_uuid=True), sa.ForeignKey("leads.id"), nullable=True),
        # No ondelete here, matching the schema doc's
        # `markup_profile_id UUID NOT NULL REFERENCES markup_profiles(id)`.
        sa.Column("markup_profile_id", UUID(as_uuid=True), sa.ForeignKey("markup_profiles.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        # Nullable, no default: subtotal/total stay NULL (not 0) until the
        # first POST /estimates/{id}/calculate run actually computes them
        # (Task 2.12) — NULL means "never calculated," matching the
        # Estimate ORM model's explicit no-default choice.
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=True),
        sa.Column("total", sa.Numeric(12, 2), nullable=True),
        # True once approved; line items (and subtotal/total) become
        # immutable — enforced at the application layer, not the DB-grant
        # layer (design decision #4, see module docstring above).
        sa.Column("is_snapshotted", sa.Boolean, nullable=False, server_default=sa.false()),
        # Nullable: unset until the Estimate is sent for signature. FK to
        # `esignatures(id)`, which now exists as of 0006 — see module
        # docstring's re-sequencing note for why 0006 had to land first.
        sa.Column("esignature_id", UUID(as_uuid=True), sa.ForeignKey("esignatures.id"), nullable=True),
        # --- The three columns below are NOT in docs/04-database-schema.md
        # Section 5's `estimates` table — a deliberate, documented extension
        # (design decision #5). See module docstring above.
        sa.Column("pdf_status", sa.String(20), nullable=False, server_default="not_requested"),
        # Nullable, STORAGE_ROOT-relative path — same convention
        # app/services/document_storage.py established in Phase 1, reused
        # directly (Task 2.13). NULL until a PDF has actually been generated.
        sa.Column("pdf_storage_path", sa.Text, nullable=True),
        sa.Column("pdf_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # Two separate CHECK constraints, mirroring the two independent
        # DB-level CHECK constraints the Estimate ORM model declares
        # (ck_estimates_status, ck_estimates_pdf_status) — same
        # belt-and-suspenders pattern as Project.ck_projects_status /
        # Lead.ck_leads_status. `status` and `pdf_status` are independent
        # lifecycles (an Estimate can be status='sent' while pdf_status=
        # 'ready' from an earlier export), hence two constraints rather than
        # one combined check.
        sa.CheckConstraint(
            "status IN ('draft','sent','approved','rejected')",
            name="ck_estimates_status",
        ),
        sa.CheckConstraint(
            "pdf_status IN ('not_requested','pending','ready','failed')",
            name="ck_estimates_pdf_status",
        ),
    )

    op.create_table(
        "estimate_line_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # ON DELETE CASCADE per the schema doc:
        # `estimate_id UUID NOT NULL REFERENCES estimates(id) ON DELETE CASCADE`
        # — deleting an Estimate deletes its line items, same shape as
        # Phase.project_id's cascade (0004).
        sa.Column("estimate_id", UUID(as_uuid=True), sa.ForeignKey("estimates.id", ondelete="CASCADE"), nullable=False),
        # No ondelete here, matching the schema doc's
        # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
        # clause) — same convention as Estimate.company_id / Project.company_id.
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        # No ondelete here, matching the schema doc's
        # `cost_catalog_item_id UUID NOT NULL REFERENCES cost_catalog_items(id)`
        # (no ON DELETE clause).
        sa.Column("cost_catalog_item_id", UUID(as_uuid=True), sa.ForeignKey("cost_catalog_items.id"), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False),
        # Copied from CostCatalogItem.unit_rate at add-time rather than
        # joined/looked-up live — intentionally a separate column, per the
        # schema doc's own Section 9 note: this is what implements the
        # historical-immutability rule. A later edit to the catalog's
        # unit_rate must NOT retroactively change what an already-built
        # Estimate shows or totals.
        sa.Column("unit_rate_snapshot", sa.Numeric(12, 2), nullable=False),
        sa.Column("line_total", sa.Numeric(12, 2), nullable=False),
        # No created_at/updated_at: docs/04-database-schema.md Section 5's
        # `estimate_line_items` table has no timestamp columns at all,
        # matching the EstimateLineItem ORM model's mixin-free choice.
    )

    # --- Row-Level Security -------------------------------------------------
    #
    # Guarded NULLIF(...)::uuid cast on every current_setting() call below —
    # see 0001's long comment for the exact connection-pooling failure mode
    # this prevents (a bare cast raises "invalid input syntax for type uuid"
    # instead of evaluating to false once a pooled connection has ever seen
    # app.current_tenant set and later returns '' rather than NULL for it).
    # Routed through get_all_descendant_ids() (0001) so a parent company's
    # session also sees its descendant branches' rows, not just its own —
    # the ordinary shape, not cost_catalog_items' bidirectional one (0005).
    # Each table gets its OWN separate policy (module docstring above) —
    # `estimate_line_items` is independently RLS-scoped by its own
    # `company_id` column, not gated through a join to `estimates`.
    for table in ("estimates", "estimate_line_items"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table} FOR ALL
            USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            """
        )

    # --- No REVOKE here (design decision #4) --------------------------------
    # Unlike 0003/0004/0006, which each REVOKEd UPDATE/DELETE at the grant
    # level for unconditionally-immutable tables, `estimates` and
    # `estimate_line_items` are conditionally immutable (only once
    # `estimates.is_snapshotted = true`) — a state a blanket grant-level
    # REVOKE cannot express. Immutability is enforced entirely in
    # application code in Tasks 2.11/2.12. See module docstring above for
    # the full rationale. app_user retains SELECT, INSERT, UPDATE, DELETE on
    # both tables via 0001's ALTER DEFAULT PRIVILEGES, unmodified by this
    # migration.


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON estimate_line_items")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON estimates")
    op.drop_table("estimate_line_items")
    op.drop_table("estimates")
