"""A tenant's own mail server.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-01

Migration 0027 let a company choose the NAME its mail goes out under, and
said the address was deliberately left alone because sending as
`@thatbuilder.com` needs that builder's DNS. This is the other half: a
company that has published SPF/DKIM for their own domain can now put their
own mail server in, and their invitations, signature requests, expiry
notices and password resets leave through it.

`password_encrypted` holds a Fernet ciphertext under the same key the
integrations module uses for OAuth tokens — this is somebody else's mail
password, and the one route that writes it never reads it back. The API
answers `has_password`.

`enabled` exists so a tenant can turn their server off without deleting
the settings, which is what somebody does while their provider is having
an outage. Off means the platform relay, the same as never having
configured anything: mail continuing to flow from the wrong domain beats
mail not flowing at all.

A tenant table like any other — RLS scoped to the tenant tree, and a
`company_id` index because the policy puts `company_id IN (...)` on every
query against it.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_email_settings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="587"),
        sa.Column("username", sa.String(255), nullable=True),
        # Fernet ciphertext, never plaintext and never returned by a route.
        sa.Column("password_encrypted", sa.Text(), nullable=True),
        sa.Column("from_address", sa.String(255), nullable=False),
        sa.Column("starttls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        # When a test message last got through. Null means "never proved",
        # which is what the screen shows so nobody assumes a saved form is
        # a working one.
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute("ALTER TABLE company_email_settings ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON company_email_settings FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )
    op.create_index(
        "ix_company_email_settings_company_id", "company_email_settings", ["company_id"]
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON company_email_settings")
    op.drop_index("ix_company_email_settings_company_id", table_name="company_email_settings")
    op.drop_table("company_email_settings")
