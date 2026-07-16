"""Refresh-token lifecycle (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md).

The ONLY module that ever sees a presentable refresh-token secret. Routes
stay thin: they call these functions and map RefreshTokenError to a
uniform 401 "Invalid refresh token" (one message for unknown, expired,
revoked, and reuse-detected alike — no oracle distinguishing them).
"""
import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import RefreshToken
from app.models.base import utcnow


class RefreshTokenError(Exception):
    """Presented token is not acceptable (unknown, expired, or revoked)."""


class RefreshTokenReuseError(RefreshTokenError):
    """Presented token was already rotated or revoked — treated as suspected
    compromise per the spec: the raiser has ALREADY revoked the whole family
    by the time this propagates. Carries user_id/family_id so the route can
    write the audit row before returning 401."""

    def __init__(self, user_id: uuid.UUID, family_id: uuid.UUID) -> None:
        super().__init__("refresh token reuse detected")
        self.user_id = user_id
        self.family_id = family_id


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def mint_refresh_token(
    session: AsyncSession, user_id: uuid.UUID, family_id: uuid.UUID | None = None
) -> tuple[RefreshToken, str]:
    """Returns (row, presentable_secret). family_id=None mints a new family
    (login); passing one keeps the rotation chain (rotate)."""
    secret = secrets.token_urlsafe(32)
    # token_hash has a unique index; a SHA-256 collision between two 256-bit
    # random secrets is ~2^-128 territory, so the IntegrityError/500 that a
    # collision would produce is accepted — a retry loop here would be
    # complexity with no realistic trigger.
    row = RefreshToken(
        user_id=user_id,
        token_hash=_hash(secret),
        family_id=family_id or uuid.uuid4(),
        expires_at=utcnow() + timedelta(days=settings.refresh_token_expire_days),
    )
    session.add(row)
    await session.flush()
    return row, secret


async def find_by_secret(session: AsyncSession, secret: str) -> RefreshToken | None:
    """Exact-hash lookup, no lock. Returns the row regardless of its
    revoked/expired state (rotate and logout each apply their own state
    rules). Rotation must NOT use this — it needs the FOR UPDATE variant
    inlined in rotate_refresh_token; see the race comment there."""
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash(secret))
    )
    return result.scalar_one_or_none()


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def rotate_refresh_token(
    session: AsyncSession, presented_secret: str
) -> tuple[RefreshToken, str]:
    """Single-use rotation. Returns (old_row, new_presentable_secret); the
    successor row is flushed in old_row.family_id's chain and old_row is
    marked revoked + replaced_by. Raises RefreshTokenReuseError (after
    revoking the family) if the token was already rotated/revoked, plain
    RefreshTokenError if unknown or expired."""
    # FOR UPDATE is load-bearing, not belt-and-suspenders: without it, two
    # concurrent rotations of the SAME token both read an un-revoked
    # snapshot under READ COMMITTED, and the ORM's PK-only UPDATE lets the
    # second one silently overwrite the first (lost update) — BOTH callers
    # get valid successors and reuse detection never fires, which is a
    # raced-refresh parallel-session attack. With the lock, the second
    # caller blocks here until the first commits, then sees the committed
    # revoked_at and correctly takes the reuse branch below.
    result = await session.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == _hash(presented_secret))
        .with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise RefreshTokenError("unknown refresh token")
    if row.revoked_at is not None or row.replaced_by_id is not None:
        # Reuse of a spent token = suspected compromise. Kill the chain
        # BEFORE raising; the route must let this UPDATE commit (the 401
        # must not roll back the containment — see the /auth/refresh route).
        await revoke_family(session, row.family_id)
        raise RefreshTokenReuseError(user_id=row.user_id, family_id=row.family_id)
    if row.expires_at <= utcnow():
        raise RefreshTokenError("expired refresh token")
    # Mint-first is safe w.r.t. ck_refresh_tokens_replaced_implies_revoked:
    # that CHECK guards the OLD row only, and its revoked_at/replaced_by_id
    # are set together below and land in a single UPDATE at the next flush.
    new_row, new_secret = await mint_refresh_token(
        session, user_id=row.user_id, family_id=row.family_id
    )
    row.revoked_at = utcnow()
    row.replaced_by_id = new_row.id
    await session.flush()
    return row, new_secret
