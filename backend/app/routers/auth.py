import uuid

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.security import create_access_token, hash_password, verify_password
from app.db import session_scope, set_current_tenant, set_current_user
from app.models import Company, CompanyUser, Subscription, User
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)
from app.services.audit import write_audit_log
from app.services.billing import TIER_INCLUDED_SEATS, get_stripe_client
from app.services.refresh_tokens import (
    RefreshTokenError,
    RefreshTokenReuseError,
    mint_refresh_token,
    rotate_refresh_token,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# Precomputed once at import time so a login attempt against an email that
# doesn't exist pays the same Argon2 verification cost as one that does —
# skipping verify_password() entirely for an unknown email is measurably
# faster (empirically ~77ms vs ~0ms) and lets an attacker enumerate
# registered emails purely from response timing, which matters for a B2B
# product where "does this company have an account" is itself sensitive.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-never-used-for-real-auth")


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest) -> RegisterResponse:
    company_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with session_scope() as session:
        async with session.begin():
            # 1. Top-level company: parent_id IS NULL, so tenant_insert's WITH
            #    CHECK passes even with no tenant context set yet (design decision #2).
            session.add(Company(id=company_id, parent_id=None, name=payload.company_name))
            await session.flush()

            # 2. users has no RLS — a global email-uniqueness lookup is legitimate.
            session.add(
                User(
                    id=user_id,
                    email=payload.admin_email,
                    password_hash=hash_password(payload.admin_password),
                    full_name=payload.admin_full_name,
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

            # 3. Now scope this transaction to the company we just created, so the
            #    company_users INSERT's WITH CHECK can see it (design decision #2).
            await set_current_tenant(session, str(company_id))
            session.add(CompanyUser(company_id=company_id, user_id=user_id, role="admin"))
            await session.flush()

            # 4. Trial subscription (Task 3.19, design spec Section 3): every
            #    new root company starts on a 14-day Pro trial. Synchronous,
            #    same transaction as the rows above — a trial-less root
            #    company isn't a state this feature tolerates; if the Stripe
            #    call fails, the whole registration transaction rolls back
            #    (the enclosing `async with session.begin():` above — not
            #    session_scope() itself, which is a bare passthrough with no
            #    commit/rollback of its own), no retry/fallback path.
            stripe_client = get_stripe_client()
            stripe_customer_id = await stripe_client.create_customer(
                email=payload.admin_email, name=payload.company_name
            )
            stripe_subscription = await stripe_client.create_trialing_subscription(
                customer_id=stripe_customer_id, tier="pro", trial_days=14
            )
            session.add(
                Subscription(
                    company_id=company_id,
                    stripe_customer_id=stripe_customer_id,
                    stripe_subscription_id=stripe_subscription.stripe_subscription_id,
                    tier="pro",
                    status=stripe_subscription.status,
                    included_seats=TIER_INCLUDED_SEATS["pro"],
                    current_period_end=stripe_subscription.current_period_end,
                )
            )
            await session.flush()

            await write_audit_log(
                session,
                company_id=company_id,
                actor_id=user_id,
                action="company.registered",
                entity_type="company",
                entity_id=company_id,
            )

    return RegisterResponse(company_id=company_id, user_id=user_id, email=payload.admin_email)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, response: Response) -> TokenResponse:
    async with session_scope() as session:
        result = await session.execute(select(User).where(User.email == payload.email))
        user = result.scalar_one_or_none()

        # Always call verify_password, even for an unknown email — against a
        # fixed dummy hash when there's no real user — so both branches pay
        # the same Argon2 cost. See _DUMMY_PASSWORD_HASH's comment above.
        password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        password_valid = verify_password(payload.password, password_hash)

        if user is None or not password_valid:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

        # Membership lookup needs app.current_user_id set for the self_membership
        # RLS policy to allow it (design decision #3).
        await set_current_user(session, str(user.id))
        result = await session.execute(
            select(CompanyUser)
            .where(CompanyUser.user_id == user.id)
            # company_id as a tiebreaker is not cosmetic: company_users has no
            # surrogate id (composite PK on company_id, user_id), and
            # created_at alone collides often enough to matter — measured at
            # ~32% of rapid successive inserts sharing a timestamp on ordinary
            # hardware, since datetime.now() resolution is coarser than
            # typical call overhead. Without a deterministic secondary key,
            # which membership .first() returns after a tie is unspecified by
            # Postgres and can differ between two logically identical queries,
            # making a user's "default company" not actually stable. Not
            # reachable today (registration only ever creates one membership),
            # but this is exactly the ordering Task 14's invitation-acceptance
            # flow will start exercising with multiple memberships per user.
            .order_by(CompanyUser.created_at, CompanyUser.company_id)
        )
        membership = result.scalars().first()
        if membership is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "User has no company memberships")

        # Refresh-token INSERT needs a commit; session_scope() never commits
        # on its own. Register can use an explicit session.begin() because it
        # begins before touching the session; here SQLAlchemy already
        # autobegan a transaction at the first execute() (the User SELECT
        # above), so a begin() would raise — commit via the session instead.
        # Post-commit attribute reads below are safe only because SessionLocal
        # sets expire_on_commit=False (app/db.py).
        _, refresh_secret = await mint_refresh_token(session, user_id=user.id)
        await session.commit()

        token = create_access_token(user_id=str(user.id), default_company_id=str(membership.company_id))
        # RFC 6749 §5.1: responses carrying tokens must not be cached.
        response.headers["Cache-Control"] = "no-store"
        return TokenResponse(
            access_token=token,
            refresh_token=refresh_secret,
            default_company_id=membership.company_id,
        )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, response: Response) -> TokenResponse:
    """Rotate a refresh token (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md).

    Reuse of a spent token is suspected compromise: the service revokes the
    whole family, and this route must COMMIT that revocation before the 401
    leaves — an exception inside the transaction would roll the containment
    back, which is why the error is carried out of the session block in a
    flag instead of raised inside it. All failure modes share one message
    (no oracle for which of unknown/expired/revoked/reused it was).
    """
    reuse_detected = False
    async with session_scope() as session:
        try:
            old_row, new_secret = await rotate_refresh_token(session, payload.refresh_token)
        except RefreshTokenReuseError as exc:
            # Audit needs a company scope; resolve it the same way login
            # resolves default_company_id. If the user has no memberships
            # left, skip the row (there is no company to file it under) —
            # the family revocation itself still commits.
            await set_current_user(session, str(exc.user_id))
            result = await session.execute(
                select(CompanyUser)
                .where(CompanyUser.user_id == exc.user_id)
                .order_by(CompanyUser.created_at, CompanyUser.company_id)
            )
            membership = result.scalars().first()
            if membership is not None:
                await set_current_tenant(session, str(membership.company_id))
                await write_audit_log(
                    session,
                    company_id=membership.company_id,
                    actor_id=exc.user_id,
                    action="auth.refresh_reuse_detected",
                    entity_type="refresh_token",
                    entity_id=exc.family_id,
                    metadata={"family_id": str(exc.family_id)},
                )
            await session.commit()
            reuse_detected = True
        except RefreshTokenError:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
        else:
            await set_current_user(session, str(old_row.user_id))
            result = await session.execute(
                select(CompanyUser)
                .where(CompanyUser.user_id == old_row.user_id)
                .order_by(CompanyUser.created_at, CompanyUser.company_id)
            )
            membership = result.scalars().first()
            if membership is None:
                # Same outcome login gives a membership-less user. The
                # rotation rolls back with the session (never committed),
                # so the presented token remains usable if memberships
                # are later restored.
                raise HTTPException(status.HTTP_403_FORBIDDEN, "User has no company memberships")
            await session.commit()
            access = create_access_token(
                user_id=str(old_row.user_id), default_company_id=str(membership.company_id)
            )
            company_id = membership.company_id
    if reuse_detected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    # RFC 6749 §5.1: responses carrying tokens must not be cached.
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(
        access_token=access, refresh_token=new_secret, default_company_id=company_id
    )
