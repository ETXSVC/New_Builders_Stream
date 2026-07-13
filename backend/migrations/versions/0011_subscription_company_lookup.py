"""Subscription company lookup: get_subscription_company_id() function for
the Stripe webhook handler's tenant-resolution problem.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-13

Task 3.21 (POST /webhooks/stripe): the webhook handler has no authenticated
caller and, more fundamentally, no tenant to scope to up front — it only
has a `stripe_subscription_id` from an inbound event payload, and needs to
find out which company that subscription belongs to BEFORE it can call
`set_current_tenant()`. But `subscriptions`' own `tenant_isolation` RLS
policy (migration 0010) requires `app.current_tenant` to already resolve to
the row's own root company before that row is even visible to a plain
`SELECT` — the exact chicken-and-egg problem this function exists to break.

Modeled directly on `get_root_company_id()` (migration 0010) and
`get_all_descendant_ids()` (migration 0001): a narrow, single-purpose,
`SECURITY DEFINER` lookup, `REVOKE`d from `PUBLIC` and `GRANT`ed only to
`app_user`, so it can read `subscriptions.company_id` regardless of the
caller's own RLS-visible scope while resolving exactly one row — not a
blanket RLS bypass. This is deliberately narrower than reusing the
Postgres table-owner (`postgres`) role for this lookup would be: it grants
exactly one UUID for exactly one lookup shape (`stripe_subscription_id ->
company_id`), not unrestricted access to `subscriptions` or any other
table, and the webhook handler's actual read/mutate of the `Subscription`
row afterward stays on the normal, RLS-scoped `app_user` session (via
`session_scope()` + `set_current_tenant()`), exactly like every other write
path in this codebase.
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION get_subscription_company_id(target_stripe_subscription_id VARCHAR(255))
        RETURNS UUID
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT company_id FROM subscriptions
            WHERE stripe_subscription_id = target_stripe_subscription_id;
        $$;
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION get_subscription_company_id(VARCHAR(255)) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION get_subscription_company_id(VARCHAR(255)) TO app_user")


def downgrade() -> None:
    op.execute("REVOKE EXECUTE ON FUNCTION get_subscription_company_id(VARCHAR(255)) FROM app_user")
    op.execute("DROP FUNCTION IF EXISTS get_subscription_company_id(VARCHAR(255))")
