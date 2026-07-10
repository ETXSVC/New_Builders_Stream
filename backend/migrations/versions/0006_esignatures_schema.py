"""Esignature schema: esignatures and its RLS policy.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-09

Per docs/04-database-schema.md Section 6. This migration deliberately runs
BEFORE the estimates/estimate_line_items migration (Task 2.8), even though
Task 2.17 (this task) is numbered after Task 2.8 in the Phase 2 plan
document — the plan's own Task 2.17 file note calls this out explicitly:
`Estimate.esignature_id` (already added to the ORM model in Task 2.7) is a
hard FK to `esignatures.id`, and Postgres requires the referenced table to
exist at CREATE TABLE time. `esignatures` must therefore land in the
migration chain before `estimates` does, regardless of the two tasks'
numbering in the plan doc — hence `down_revision = "0005"` here, with
Task 2.8's estimates migration expected to chain onto "0006" next.

`esignatures` is a plain, flat, company-scoped table (no hierarchy concern
of its own, same as `leads`/`markup_profiles`), so it gets the ordinary
`tenant_isolation` policy shape every non-inheriting Phase 1/2 table uses: a
single FOR ALL policy with both USING and WITH CHECK, each using the guarded
NULLIF(current_setting('app.current_tenant', true), '')::uuid cast (see
0001's long comment for why the guard is required — a bare cast raises an
unhandled 500 once a pooled connection has ever seen app.current_tenant set,
because current_setting(name, true) returns '' rather than NULL on a
connection that previously had the GUC set and later rolled back to no
value), gated through get_all_descendant_ids() so a parent company's session
also sees its descendant branches' rows. This is NOT the bidirectional shape
0005 gave cost_catalog_items — esignatures has no inheritance concept, so
the ordinary shape is correct here.

esignatures is the one genuinely UNCONDITIONALLY immutable table in Phase 2
(Security & Compliance Section 7: retained "indefinitely," "never deleted,
even if the underlying Project or company is later deactivated"). Unlike
`estimates`/`estimate_line_items`, whose immutability is conditional on
Estimate.is_snapshotted's runtime value and therefore can't be expressed as
a blanket grant-level REVOKE (design decision #4), an esignature row is
immutable from the instant it's written — there is no "before approval"
state for it to have, so it gets the exact same Phase 1 grant-level
hardening `communication_logs` (0003) and `daily_logs`/`documents` (0004)
received (design decision #6 in the Phase 1 plan, extended here to Phase 2):
`REVOKE UPDATE, DELETE ON esignatures FROM app_user`.

Verified empirically against the live Postgres container (see task notes)
that app_user already has SELECT, INSERT, UPDATE, DELETE on the new table
without any explicit GRANT here — migrations run as the `postgres` owner
role (design decision #1; MIGRATIONS_DATABASE_URL connects as postgres),
and 0001's `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT,
UPDATE, DELETE ON TABLES TO app_user` was issued by that same role, so it
applies automatically to every table `postgres` creates afterward in this
schema, including this one. No explicit GRANT statement is needed before
the REVOKE.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, UUID

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "esignatures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # No ondelete: matches the schema doc's
        # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
        # clause) — same convention as Project.company_id / MarkupProfile.company_id.
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("signer_name", sa.String(255), nullable=False),
        sa.Column("signer_email", sa.String(255), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_address", INET, nullable=False),
        # Rendered signature image/hash, retained per the Security &
        # Compliance doc (schema doc's inline comment).
        sa.Column("signature_artifact_path", sa.Text, nullable=False),
        sa.Column("document_type", sa.String(20), nullable=False),
        # No created_at/updated_at: docs/04-database-schema.md Section 6's
        # `esignatures` table has no timestamp columns beyond signed_at
        # itself, matching the Esignature ORM model's mixin-free choice
        # (module docstring above).
        sa.CheckConstraint(
            "document_type IN ('estimate','change_order')",
            name="ck_esignatures_document_type",
        ),
    )

    # --- Row-Level Security -------------------------------------------------
    #
    # Guarded NULLIF(...)::uuid cast — see 0001's long comment for the exact
    # connection-pooling failure mode this prevents (a bare cast raises
    # "invalid input syntax for type uuid" instead of evaluating to false
    # once a pooled connection has ever seen app.current_tenant set and
    # later returns '' rather than NULL for it). Routed through
    # get_all_descendant_ids() (0001) so a parent company's session also
    # sees its descendant branches' rows, not just its own — the ordinary
    # shape, not cost_catalog_items' bidirectional one (module docstring).
    op.execute("ALTER TABLE esignatures ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON esignatures FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- Immutability hardening (design decision #6, extended to Phase 2) ---
    # app_user already has SELECT, INSERT, UPDATE, DELETE on the new table
    # via 0001's ALTER DEFAULT PRIVILEGES (verified empirically — see module
    # docstring). Revoke the two mutation privileges esignatures must never
    # allow: unlike estimates/estimate_line_items (immutability conditional
    # on is_snapshotted, application-layer enforced — design decision #4),
    # esignatures rows are immutable from the moment they're written, full
    # stop, so they get the same unconditional grant-level REVOKE as
    # communication_logs (0003) and daily_logs/documents (0004).
    op.execute("REVOKE UPDATE, DELETE ON esignatures FROM app_user")


def downgrade() -> None:
    op.execute("GRANT UPDATE, DELETE ON esignatures TO app_user")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON esignatures")
    op.drop_table("esignatures")
