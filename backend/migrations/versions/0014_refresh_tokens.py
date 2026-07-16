"""refresh_tokens: hashed opaque refresh tokens with rotation families.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-16

Per docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md.

Deliberately NO RLS on this table — it is user-scoped, not tenant-scoped
(one user can belong to several companies; the token belongs to the
person), exactly like `users` itself, and token rows/hashes are never
serialized into any API response. Every query filters on an exact
token_hash or user_id server-side.

app_user keeps SELECT/INSERT/UPDATE from 0001's blanket grants (lines
138-141: GRANT ... ON ALL TABLES plus ALTER DEFAULT PRIVILEGES) but loses
DELETE: revocation is an UPDATE (revoked_at), and revoked rows are
retained as evidence. Same REVOKE mechanism as 0006's esignatures
hardening — which went further and revoked UPDATE too; audit_log, by
contrast, is append-only by application convention only, with no
grant-level enforcement. The CHECK constraint enforces the rotation
invariant that matters most: a token with a successor (replaced_by_id
set) must be revoked — the alternative is a redeemable token that has
already been rotated past, i.e. a double-spend.

Future pruning job (deferred by the spec): app_user cannot DELETE at all,
so pruning needs the owner role (or a targeted GRANT); it should delete
oldest-first so the replaced_by_id self-FK's referencing rows go before
their targets, and at that point "retained as evidence" above stops being
unconditionally true — update this docstring then.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("family_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "replaced_by_id",
            UUID(as_uuid=True),
            sa.ForeignKey("refresh_tokens.id"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "replaced_by_id IS NULL OR revoked_at IS NOT NULL",
            name="ck_refresh_tokens_replaced_implies_revoked",
        ),
    )
    op.create_index("idx_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("idx_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.execute("REVOKE DELETE ON refresh_tokens FROM app_user")


def downgrade() -> None:
    op.drop_table("refresh_tokens")
