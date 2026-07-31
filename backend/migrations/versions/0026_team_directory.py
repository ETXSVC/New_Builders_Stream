"""Team directory: per-company profiles for the people in a company.

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-30

Until now a company's people were a `company_users` row — an id and a role,
nothing else. There was nowhere to record a phone number, an address, whose
trade is what, or a photo, and no screen to manage any of it. This adds the
storage for that.

THE PROFILE HANGS OFF THE MEMBERSHIP, NOT THE USER, and that is the whole
design decision. `users` deliberately carries NO row-level security — it is
global, and `get_current_user` looks a row up in it before any tenant
context exists. Putting a home address or a manager's private notes there
would make them readable by every company that person also belongs to. A
subcontractor's bookkeeper who works for three builders would leak between
all three. So the profile is a TENANT table keyed by
`(company_id, user_id)`, and each company keeps its own record of the same
human.

The composite foreign key into `company_users(company_id, user_id)` with
ON DELETE CASCADE is what stops a profile outliving the membership it
describes: offboarding somebody removes their details with them, rather
than leaving an orphaned address behind that no screen shows and no query
cleans up.

WHAT IS DELIBERATELY NOT HERE:

- **Email.** It lives on `users` because it is the login. Duplicating it
  per company would create two answers to "what is their address" and no
  rule for which wins.
- **A `subcontractor` flag.** The existing `subcontractors` table (external
  trades, with compliance documents hanging off it) stays its own module.
  These are the company's own people.
- **Any tie to `users.full_name`.** That is the name the account holder set
  for themselves; `first_name`/`last_name` here are the company's record of
  them and are separately editable, because an employer's directory and a
  person's own account are not the same thing.

`professions` is company-managed rather than a constant in code, so adding
"Drywall Finisher" is a row rather than a migration. Uniqueness is
case-insensitive per company — "Electrician" and "electrician" in the same
dropdown is a data-entry bug, not a choice.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_TABLES = ("professions", "member_profiles", "member_phones")


def upgrade() -> None:
    op.create_table(
        "professions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # Case-insensitive per company: a dropdown offering both "Electrician"
    # and "electrician" is a data-entry bug rather than a feature. A
    # functional unique index rather than a UNIQUE constraint because the
    # latter cannot take an expression.
    op.execute(
        "CREATE UNIQUE INDEX uq_professions_company_name ON professions (company_id, lower(name))"
    )

    op.create_table(
        "member_profiles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("first_name", sa.String(100), nullable=True),
        sa.Column("last_name", sa.String(100), nullable=True),
        sa.Column("address_line1", sa.String(255), nullable=True),
        sa.Column("address_line2", sa.String(255), nullable=True),
        sa.Column("city", sa.String(100), nullable=True),
        sa.Column("state", sa.String(100), nullable=True),
        sa.Column("postal_code", sa.String(20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "profession_id",
            UUID(as_uuid=True),
            # SET NULL rather than RESTRICT: deleting a profession should
            # not be blocked by whoever happens to hold it, and should not
            # silently delete a person either.
            sa.ForeignKey("professions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Relative to STORAGE_ROOT, exactly as company branding logos are —
        # never an absolute path, so the volume can move.
        sa.Column("image_path", sa.String(500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # One profile per person per company.
        sa.UniqueConstraint("company_id", "user_id", name="uq_member_profiles_company_user"),
        # The membership is what this describes, so it is what this depends
        # on. CASCADE means offboarding somebody takes their details with
        # them instead of orphaning an address no screen ever shows again.
        sa.ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["company_users.company_id", "company_users.user_id"],
            name="fk_member_profiles_membership",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "member_phones",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column(
            "member_profile_id",
            UUID(as_uuid=True),
            sa.ForeignKey("member_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Free text rather than an enum: "site trailer" and "spouse" are
        # both things a builder writes down, and neither belongs in a CHECK
        # constraint.
        sa.Column("label", sa.String(50), nullable=True),
        # Also free text, and deliberately not normalised. Numbers get typed
        # with extensions, country codes and parentheses; storing what was
        # entered beats storing a lossy interpretation of it.
        sa.Column("number", sa.String(40), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_member_phones_profile", "member_phones", ["member_profile_id"]
    )

    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table} FOR ALL
            USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            """
        )
        # Required, not an optimization: the policy above puts
        # `company_id IN (...)` on every query against this table, and
        # `test_company_id_index_coverage.py` sweeps for exactly this.
        op.create_index(f"ix_{table}_company_id", table, ["company_id"])


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.drop_index(f"ix_{table}_company_id", table_name=table)

    op.drop_index("ix_member_phones_profile", table_name="member_phones")
    op.drop_table("member_phones")
    op.drop_table("member_profiles")
    op.execute("DROP INDEX IF EXISTS uq_professions_company_name")
    op.drop_table("professions")
