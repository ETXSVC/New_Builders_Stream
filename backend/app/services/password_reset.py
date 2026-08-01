"""Password-reset token lifecycle (migration 0028).

Deliberately the same shape as `app/services/refresh_tokens.py`: the only
module that ever sees a presentable secret, with routes staying thin and
mapping one error type to one uniform response.

THE RULES THIS FILE EXISTS TO KEEP, each of which is a way password reset
is commonly got wrong:

* **The secret is never stored.** Only its SHA-256, so a leak of this table
  is not a set of working reset links.
* **Issuing invalidates the outstanding ones.** Somebody who clicks
  "forgot password" three times because nothing arrived should not end up
  with three live keys to their account; only the newest works.
* **Redemption is atomic and single-use.** `used_at` is stamped in the same
  transaction that rewrites the password, so a double-submitted link cannot
  set two passwords, and a link that reaches an attacker after the fact is
  spent.
* **Nothing here says whether an address exists.** That decision lives in
  the route, but the helpers are shaped to make it easy: `issue_for_email`
  returns None for an unknown address instead of raising, so the caller's
  success path is identical either way.
"""
import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PasswordResetToken, User
from app.models.base import utcnow

# One hour. Short because this opens an account that already exists —
# unlike an invitation (seven days), which creates one that does not.
RESET_TOKEN_TTL = timedelta(hours=1)


class PasswordResetError(Exception):
    """The presented token is not redeemable — unknown, expired, or spent.

    One exception for all three, and the route maps it to one message: an
    attacker holding a guessed token must not learn which of those it was.
    """


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def issue_for_email(session: AsyncSession, email: str) -> tuple[User, str] | None:
    """Mint a reset token for `email`, or None if nobody has that address.

    None rather than an exception because "no such user" is not an error
    here — it is the case the route must handle identically to success, so
    that the response cannot be used to enumerate accounts.
    """
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        return None

    # Every earlier outstanding link dies now. Marked used rather than
    # deleted: `app_user` holds no DELETE on this table (migration 0028),
    # and a spent row is evidence of a reset that was requested.
    await session.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=utcnow())
    )

    secret = secrets.token_urlsafe(32)
    session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash(secret),
            expires_at=utcnow() + RESET_TOKEN_TTL,
        )
    )
    await session.flush()
    return user, secret


async def redeem(session: AsyncSession, secret: str) -> User:
    """Spend `secret` and hand back the user it belongs to.

    Raises `PasswordResetError` for unknown, expired and already-used
    alike. The caller sets the new password on the returned user in this
    same transaction — the row is marked used here, before that, so the two
    commit together or not at all.
    """
    token = (
        await session.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == _hash(secret))
        )
    ).scalar_one_or_none()

    if token is None or token.used_at is not None or token.expires_at <= utcnow():
        raise PasswordResetError("password reset token is not redeemable")

    token.used_at = utcnow()
    user = await session.get(User, token.user_id)
    if user is None:
        # The FK cascades, so this is unreachable rather than merely
        # unlikely — asserted as a failure rather than silently treated as
        # a valid reset for nobody.
        raise PasswordResetError("password reset token has no user")
    return user


async def outstanding_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Redeemable links for a user. Test-facing, and cheap enough to keep
    honest: the invalidate-on-reissue rule above is invisible otherwise."""
    rows = (
        await session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > utcnow(),
            )
        )
    ).scalars().all()
    return len(rows)
