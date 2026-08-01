"""Password reset: a single-use, expiring credential sent by email.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-01

`auth.py` had register, login, refresh, change-password, MFA and logout —
and no way back in for somebody who forgot their password. There was no
in-product recovery at all: not for the user, and not for an operator
either, since no console route writes `users`. A forgotten password meant
an admin editing the database, or a lost account.

SHAPED LIKE `refresh_tokens` (migration 0014), because it is the same kind
of thing — a bearer secret with a lifetime — and the same rules apply:

- **Only the hash is stored.** `token_hash` is SHA-256 of a
  `secrets.token_urlsafe(32)` secret that exists in one email and nowhere
  else. A dump of this table hands an attacker nothing usable, which is
  the whole reason not to store the token itself.
- **Single use**, via `used_at`. A reset link in an inbox is a live
  credential to an account; leaving it replayable would make every
  historical reset email a standing key.
- **Short-lived.** An hour, not the invitation's seven days: this opens an
  account that already exists, rather than creating one.
- **`app_user` cannot DELETE**, matching `refresh_tokens` — spent and
  expired rows are evidence, and the runtime role has no business erasing
  them.

NO `company_id`, and therefore no RLS policy: a reset happens before any
session exists, so there is no tenant to scope it to — the same reasoning
that leaves `users` and `refresh_tokens` outside the tenant model.
`tests/test_rls_policy_coverage.py` records that decision explicitly rather
than letting an unpoliced table pass unnoticed.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            # CASCADE like refresh_tokens: a hard-deleted user takes their
            # outstanding reset links with them.
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The lookup every confirm does (by user, to invalidate the rest) and
    # the sweep a future cleanup job would do.
    op.create_index("idx_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
    op.execute("REVOKE DELETE ON password_reset_tokens FROM app_user")


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
