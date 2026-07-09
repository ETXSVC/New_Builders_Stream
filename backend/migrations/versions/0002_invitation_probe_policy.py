"""Add a bootstrap RLS policy for invitation acceptance.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-08

Design decision #9 (see plan doc): accept_invitation (Task 14) needs to look
up an Invitation by id BEFORE any tenant context exists — the invitee isn't a
member of any company yet, so there's no `app.current_tenant` to set ahead of
time. `invitations` previously had only the `tenant_isolation` FOR ALL policy
from migration 0001, which requires `app.current_tenant` to already be set to
resolve any rows at all. With no tenant context set, that policy's USING
clause reduces to `company_id IN (SELECT ... FROM get_all_descendant_ids(NULL))`,
which is always empty — so the very first probe query in accept_invitation
(`SELECT company_id FROM invitations WHERE id = :id`) returns nothing for
EVERY invitation, valid or not, and the endpoint 404s unconditionally. This
was verified empirically against the live Postgres container: with only the
0001 policies applied, accept_invitation's probe returns zero rows for a
just-created, unexpired invitation.

This mirrors the exact problem company_users had for membership lookups
(design decision #3), and the fix follows the same established pattern: a
second, independently-scoped PERMISSIVE policy for SELECT only. Where
self_membership keys on `app.current_user_id` (a fact the caller has already
proven by presenting a valid JWT), this new `invitation_probe` policy keys on
a new GUC, `app.probing_invitation_id`, set by the caller to the exact
invitation_id it's asking about immediately before the probe query. Postgres
ORs permissive policies of the same command together, so this doesn't weaken
isolation for any other query path — it only ever allows a session to see
the single invitation row whose id it already explicitly supplied. An unknown
or mismatched id can never satisfy `id = NULLIF(current_setting(...), '')::uuid`,
so the probe for a nonexistent invitation still correctly returns nothing.
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE POLICY invitation_probe ON invitations FOR SELECT
        USING (id = NULLIF(current_setting('app.probing_invitation_id', true), '')::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS invitation_probe ON invitations")
