"""Project Management schema: projects, phases, tasks, documents,
daily_logs, and their RLS policies.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-09

Per docs/04-database-schema.md Section 4, excluding `change_orders`
(Phase 1 plan's "Explicitly Out of Scope" section: `change_orders` has a
hard FK to `esignatures(id)`, a table that doesn't exist until Phase 2's
e-signature capability ships).

All five tables are flat, company-scoped tables (no hierarchy concern of
their own, same as `leads`/`communication_logs` in 0003), so their
`tenant_isolation` policy follows the exact same pattern as 0003: a single
FOR ALL policy with both USING and WITH CHECK, each using the guarded
NULLIF(current_setting('app.current_tenant', true), '')::uuid cast, gated
through get_all_descendant_ids() (0001) so a parent company's session sees
rows belonging to its own id AND every descendant branch's id — this is
the mechanism that gives Project Management its hierarchical visibility
(Phase 1 plan Task 1.17 calls this out explicitly for these tables). See
0001's long comment for why the NULLIF guard is required (a bare cast
raises an unhandled 500 once a pooled connection has ever seen
app.current_tenant set) and why WITH CHECK is not optional (USING alone
only gates which existing row a caller may target, not what values may be
written into it).

`daily_logs` and `documents` are immutable by design (schema doc:
"Immutable once submitted (application-layer enforced)" for daily_logs;
documents versions by inserting a new row rather than ever updating an
existing one) — this migration hardens both at the grant level too
(design decision #6 in the Phase 1 plan): `REVOKE UPDATE, DELETE ON
daily_logs, documents FROM app_user`. `projects`, `phases`, and `tasks`
are deliberately left untouched: `projects` has a status state machine and
general-field PATCH (Task 1.12/1.13), and `tasks` has status/assignee
updates (Task 1.14) — both are ordinary mutable entities.

Verified empirically against the live Postgres container (see task
notes) that app_user already has SELECT, INSERT, UPDATE, DELETE on all
five new tables without any explicit GRANT here — migrations run as the
`postgres` owner role (design decision #1; MIGRATIONS_DATABASE_URL
connects as postgres), and 0001's `ALTER DEFAULT PRIVILEGES IN SCHEMA
public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user` was
issued by that same role, so it applies automatically to every table
`postgres` creates afterward in this schema, including these five. No
explicit GRANT statement is needed before the REVOKE.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("lead_id", UUID(as_uuid=True), sa.ForeignKey("leads.id"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("site_address", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("projected_start_date", sa.Date, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('draft','pre_construction','active','suspended','completed','archived')",
            name="ck_projects_status",
        ),
    )

    op.create_table(
        "phases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False, server_default="0"),
        # No created_at/updated_at: docs/04-database-schema.md Section 4's
        # `phases` table has no timestamp columns at all.
    )

    op.create_table(
        "tasks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("phase_id", UUID(as_uuid=True), sa.ForeignKey("phases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("assignee_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # No updated_at: docs/04-database-schema.md Section 4's `tasks` table
        # has only created_at (tasks is not immutable, it just isn't
        # last-modified-tracked at the DB level).
        sa.CheckConstraint(
            "status IN ('open','in_progress','done')",
            name="ck_tasks_status",
        ),
    )

    op.create_table(
        "documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("uploaded_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # No updated_at column: new versions are new rows (design decision
        # #6), never an UPDATE of an existing row.
    )

    op.create_table(
        "daily_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("author_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("log_date", sa.Date, nullable=False),
        sa.Column("weather", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # Immutable once submitted (application-layer enforced per the
        # schema doc's comment; DB-level REVOKE UPDATE, DELETE hardening
        # below).
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
    # this is the hierarchical-visibility mechanism Task 1.17 exercises.
    for table in ("projects", "phases", "tasks", "documents", "daily_logs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table} FOR ALL
            USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            """
        )

    # --- Immutability hardening (design decision #6) ------------------------
    # app_user already has SELECT, INSERT, UPDATE, DELETE on all five new
    # tables via 0001's ALTER DEFAULT PRIVILEGES (verified empirically — see
    # module docstring). Revoke the two mutation privileges daily_logs and
    # documents must never allow, so DB-level enforcement backs up the fact
    # that no update/delete route for either will ever exist. projects,
    # phases, and tasks are deliberately left untouched: projects has a
    # status state machine and general-field PATCH (Task 1.12/1.13), tasks
    # has status/assignee updates (Task 1.14) — both are ordinary mutable
    # entities, unlike daily_logs (immutable once submitted) and documents
    # (new versions are new rows, never an UPDATE of an existing one).
    op.execute("REVOKE UPDATE, DELETE ON daily_logs, documents FROM app_user")


def downgrade() -> None:
    op.execute("GRANT UPDATE, DELETE ON daily_logs, documents TO app_user")
    for table in ("projects", "phases", "tasks", "documents", "daily_logs"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("daily_logs")
    op.drop_table("documents")
    op.drop_table("tasks")
    op.drop_table("phases")
    op.drop_table("projects")
