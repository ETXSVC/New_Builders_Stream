"""refresh_tokens: hashed opaque refresh tokens with rotation families.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-16

Per docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md.

Deliberately NO RLS on this table — it is user-scoped, not tenant-scoped
(one user can belong to several companies; the token belongs to the
person), exactly like `users` itself, and no API route ever reads it back.
Every query filters on an exact token_hash or user_id server-side.

app_user keeps SELECT/INSERT/UPDATE from 0001's ALTER DEFAULT PRIVILEGES
but loses DELETE: revocation is an UPDATE (revoked_at), and revoked rows
are retained as evidence — same append-only posture as audit_log, applied
with REVOKE like 0006 did for esignatures.
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
        sa.Column("token_hash", sa.CHAR(64), nullable=False, unique=True),
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
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.execute("REVOKE DELETE ON refresh_tokens FROM app_user")


def downgrade() -> None:
    op.drop_table("refresh_tokens")
