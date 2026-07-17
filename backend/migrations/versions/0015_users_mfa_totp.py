"""users MFA/TOTP columns: encrypted secret, activation timestamp, replay step.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-16

Per docs/superpowers/specs/2026-07-16-mfa-totp-design.md Decision 4: three
nullable columns on users (no separate table — 1:1 user state, no history
requirement). users has no RLS (0001) and app_user's blanket grants cover
the new columns; nothing else to do here.
"""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret_encrypted", sa.Text, nullable=True))
    op.add_column("users", sa.Column("mfa_activated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("totp_last_used_step", sa.BigInteger, nullable=True))


def downgrade() -> None:
    op.drop_column("users", "totp_last_used_step")
    op.drop_column("users", "mfa_activated_at")
    op.drop_column("users", "totp_secret_encrypted")
