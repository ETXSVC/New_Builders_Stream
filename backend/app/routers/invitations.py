import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.security import hash_password
from app.db import session_scope, set_current_tenant, set_invitation_probe
from app.models import CompanyUser, Invitation, User
from app.schemas.invitation import InvitationAcceptRequest, InvitationCreateRequest, InvitationResponse
from app.services.audit import write_audit_log

router = APIRouter(prefix="/invitations", tags=["invitations"])

INVITATION_TTL_DAYS = 7


@router.post("", response_model=InvitationResponse, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    payload: InvitationCreateRequest, current: CurrentUser = Depends(require_role("admin"))
) -> InvitationResponse:
    invitation = Invitation(
        company_id=current.company_id,
        email=payload.email,
        role=payload.role,
        expires_at=datetime.now(timezone.utc) + timedelta(days=INVITATION_TTL_DAYS),
    )
    current.session.add(invitation)
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="invitation.created",
        entity_type="invitation",
        entity_id=invitation.id,
        metadata={"email": payload.email, "role": payload.role},
    )
    # No explicit commit here — get_current_user (design decision #8) commits
    # current.session once, after this handler returns.

    return InvitationResponse.model_validate(invitation)


@router.post("/{invitation_id}/accept", response_model=InvitationResponse)
async def accept_invitation(invitation_id: uuid.UUID, payload: InvitationAcceptRequest) -> InvitationResponse:
    async with session_scope() as session:
        async with session.begin():
            # Invitation acceptance happens before the invitee has any tenant
            # membership, so this lookup can't go through the normal
            # tenant-scoped path. It looks up by primary key only, which
            # PostgreSQL RLS still restricts unless we're inside the right
            # tenant context — so we scope to this invitation's own company
            # up front. This is safe: the only thing an attacker controls is
            # the invitation_id itself, and a wrong/unknown ID simply finds
            # nothing after the SET, matching the 404 below.
            #
            # set_invitation_probe (design decision #9, migration 0002) scopes
            # a second, narrowly-permissive RLS policy on invitations —
            # invitation_probe, mirroring company_users' self_membership
            # policy — to exactly this invitation_id for the rest of this
            # transaction. Without it, invitations' sole tenant_isolation
            # policy blocks this SELECT outright (no tenant context exists
            # yet), and every accept request 404s regardless of whether the
            # invitation actually exists. Verified empirically: without this
            # call, both accept-invitation tests below failed with 404
            # instead of 200/410.
            await set_invitation_probe(session, str(invitation_id))

            probe = await session.execute(
                select(Invitation.company_id).where(Invitation.id == invitation_id)
            )
            company_id = probe.scalar_one_or_none()
            if company_id is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found")

            await set_current_tenant(session, str(company_id))

            result = await session.execute(select(Invitation).where(Invitation.id == invitation_id))
            invitation = result.scalar_one()

            if invitation.accepted_at is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, "Invitation already accepted")
            if invitation.expires_at < datetime.now(timezone.utc):
                raise HTTPException(status.HTTP_410_GONE, "Invitation has expired")

            user = User(
                email=invitation.email,
                password_hash=hash_password(payload.password),
                full_name=payload.full_name,
            )
            session.add(user)
            try:
                await session.flush()
            except IntegrityError:
                raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

            session.add(CompanyUser(company_id=invitation.company_id, user_id=user.id, role=invitation.role))
            invitation.accepted_at = datetime.now(timezone.utc)
            await session.flush()

            await write_audit_log(
                session,
                company_id=invitation.company_id,
                actor_id=user.id,
                action="invitation.accepted",
                entity_type="invitation",
                entity_id=invitation.id,
            )

            return InvitationResponse.model_validate(invitation)
