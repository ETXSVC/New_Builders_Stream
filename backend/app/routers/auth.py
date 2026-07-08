import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.security import create_access_token, hash_password, verify_password
from app.db import session_scope, set_current_tenant
from app.models import Company, CompanyUser, User
from app.schemas.auth import LoginRequest, RegisterRequest, RegisterResponse, TokenResponse
from app.services.audit import write_audit_log

router = APIRouter(prefix="/auth", tags=["auth"])


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
async def login(payload: LoginRequest) -> TokenResponse:
    async with session_scope() as session:
        result = await session.execute(select(User).where(User.email == payload.email))
        user = result.scalar_one_or_none()
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

        # Membership lookup needs app.current_user_id set for the self_membership
        # RLS policy to allow it (design decision #3).
        from app.db import set_current_user

        await set_current_user(session, str(user.id))
        result = await session.execute(
            select(CompanyUser).where(CompanyUser.user_id == user.id).order_by(CompanyUser.created_at)
        )
        membership = result.scalars().first()
        if membership is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "User has no company memberships")

        token = create_access_token(user_id=str(user.id), default_company_id=str(membership.company_id))
        return TokenResponse(access_token=token, default_company_id=membership.company_id)
