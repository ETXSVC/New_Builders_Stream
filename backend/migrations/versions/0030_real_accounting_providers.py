"""What a real QuickBooks/FreshBooks connection needs that a fake one did not.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-01

The integrations spec predicted the swap from the fake accounting client to
real ones would be "only the factory changes", then corrected itself in its
own Open Questions: it needs a migration. This is that migration, and it
carries two things the fake never needed.

**`integration_connections.provider_account_id`.** Neither provider's access
token is enough to address an API on its own. QuickBooks returns a
`realmId` — the id of the company file — as a query parameter on the OAuth
*callback*, not in the token exchange response, and it is a path segment of
every subsequent request. FreshBooks has the same shape under a different
name: an `accountId` fetched from `/auth/api/v1/users/me` after the
exchange. Both are per-connection facts, learned once at connect time, and
without them a stored token addresses nothing. One nullable column holds
either, because that is what it is — the provider's own id for the account
this connection points at — and a connection made before this migration
legitimately does not have one yet.

**`integration_entity_mappings`.** The blocker that was not in the spec at
all: a QuickBooks invoice cannot be created without a `CustomerRef` naming
a real Customer in the tenant's own books, and a bill needs a `VendorRef`.
Builders Stream has neither — `invoices` has no client column, only
`project_id` — so the sync path has to resolve a local name to a provider
id and remember the answer. Remembering it is the point of this table:
resolution otherwise costs a search API call per record per sync, and worse,
a find-or-create that forgets is a find-or-create that eventually creates a
duplicate customer in somebody's accounting file.

Deliberately ONE table with an `entity_kind` discriminator rather than three
(`customers`/`vendors`/`accounts`): the rows are identical in shape and the
lookup is identical in every case, so three tables would be three copies of
the same RLS policy, index and upsert. `local_key` is the display name that
was matched on rather than a foreign key, because what is being mapped is
not always a row here — a bill's vendor is free text on `bills.vendor_name`,
and an expense account is a provider-side concept with no local counterpart
at all.

A tenant table like any other: RLS scoped to the tenant tree, and a
`company_id` index because the policy puts `company_id IN (...)` on every
query against it.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, with no backfill: a connection made before this migration
    # genuinely does not know its realm/account id, and inventing one would
    # be worse than a sync that fails with "reconnect this provider".
    op.add_column(
        "integration_connections",
        sa.Column("provider_account_id", sa.String(100), nullable=True),
    )

    op.create_table(
        "integration_entity_mappings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id"),
            nullable=False,
        ),
        # ON DELETE CASCADE: these ids are meaningless once the connection
        # they were resolved through is gone, and a reconnect to a DIFFERENT
        # company file would otherwise inherit the previous file's ids and
        # post invoices against strangers.
        sa.Column(
            "connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("integration_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 'customer' | 'vendor' | 'account'
        sa.Column("entity_kind", sa.String(20), nullable=False),
        # The local display name that was matched on — a client's full name,
        # a bill's vendor_name, or a well-known account role like 'income'.
        sa.Column("local_key", sa.String(255), nullable=False),
        sa.Column("provider_entity_id", sa.String(100), nullable=False),
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
        # Scoped to the CONNECTION, not the company: a company that
        # disconnects QuickBooks and connects a different file must not
        # reuse the old file's ids, and one that connects both providers
        # keeps two independent maps for the same names.
        sa.UniqueConstraint(
            "connection_id",
            "entity_kind",
            "local_key",
            name="uq_integration_entity_mappings_connection_kind_key",
        ),
    )

    op.execute("ALTER TABLE integration_entity_mappings ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON integration_entity_mappings FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )
    op.create_index(
        "ix_integration_entity_mappings_company_id",
        "integration_entity_mappings",
        ["company_id"],
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON integration_entity_mappings")
    op.drop_index(
        "ix_integration_entity_mappings_company_id",
        table_name="integration_entity_mappings",
    )
    op.drop_table("integration_entity_mappings")
    op.drop_column("integration_connections", "provider_account_id")
