"""Belonging to more than one company, and staying where you switched to.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-01

`companies.parent_id` has formed a tenant hierarchy since migration 0001 and
every RLS policy in the schema is built on it, but no user has ever had more
than one membership: the only three places that create a `company_users` row
(`auth.register`, `invitations.accept`, `platform_tenants`) each create one
alongside a brand-new `users` row, and accepting an invitation for an email
that already exists returns 409. So "which company am I acting as" has never
been a question anyone could answer two ways.

This migration is the schema half of making it one.

**`refresh_tokens.active_company_id`.** The access token carries
`default_company_id`, and `auth.refresh` re-derives it from
`_default_membership` on every rotation — deliberately, because a token
minted at login and one rotated at refresh must not disagree. That rule is
kept, but its *source* has to change: without somewhere durable to record
the company a user switched to, the switch would silently revert at the next
refresh, roughly fourteen minutes later, and the user would find themselves
back in their default company mid-task with no error. The refresh-token
chain is the right home because it is already the thing that survives an
access token and is already rotated as one unit.

Nullable, and null means "use the default membership" — which is what every
row that predates this migration means, and what a session that has never
switched means. No backfill, because there is nothing to back-fill *to*.

ON DELETE SET NULL rather than CASCADE: losing a company should log you out
of that company, not destroy your refresh-token chain and sign you out
everywhere.

**`companies.self_membership`.** `company_users` has had a `self_membership`
policy since 0001 so `get_current_user` can read the caller's own
memberships before any tenant context exists. Listing those memberships
needs their company NAMES too, and `companies` is scoped to
`get_all_descendant_ids(app.current_tenant)` — which, by construction,
cannot contain a company in an unrelated tree. That is exactly the set a
switcher exists to show.

So `companies` gets the mirror of the policy `company_users` already has:
SELECT only, scoped to `app.current_user_id`, matching rows the caller
genuinely holds a membership for. It widens nothing else — permissive
policies are ORed, and this one can only ever add companies the user is
already a member of, which they can already name via `company_users`.
`tests/test_rls_policy_coverage.py`'s allowlist gains the entry, so this is
a reviewed exception rather than a quiet one.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "refresh_tokens",
        sa.Column(
            "active_company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    op.execute(
        """
        CREATE POLICY self_membership ON companies FOR SELECT
        USING (id IN (
            SELECT company_id FROM company_users
            WHERE user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        ))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS self_membership ON companies")
    op.drop_column("refresh_tokens", "active_company_id")
