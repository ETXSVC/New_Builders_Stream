import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.security import hash_password
from app.db import session_scope, set_current_tenant, set_invitation_probe
from app.models import Company, CompanyUser, Invitation, User
from app.schemas.invitation import (
    InvitationAcceptRequest,
    InvitationCreateRequest,
    InvitationListResponse,
    InvitationResponse,
)
from app.services.audit import write_audit_log
from app.tasks.send_invitation_email import send_invitation_email

router = APIRouter(prefix="/invitations", tags=["invitations"])

INVITATION_TTL_DAYS = 7


@router.post("", response_model=InvitationResponse, status_code=status.HTTP_201_CREATED)
async def create_invitation(
    payload: InvitationCreateRequest,
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
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
    # Email delivery: enqueued to the Dramatiq worker rather than sent
    # inline — a slow/unreachable SMTP server must not stall or fail the
    # request, and the worker's retry/backoff covers transient failures.
    # The payload carries everything the email needs (see the actor's own
    # docstring for why it deliberately does no DB reads). The accept URL
    # reuses frontend_base_url the same way the integrations OAuth
    # callback's redirect does. company name comes from the acting tenant's
    # own row (RLS-visible by construction).
    company_name = await current.session.scalar(
        select(Company.name).where(Company.id == current.company_id)
    )
    send_invitation_email.send(
        to_email=payload.email,
        company_name=company_name or "your team",
        role=payload.role,
        accept_url=f"{settings.frontend_base_url}/accept-invitation?id={invitation.id}",
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
            # tenant-scoped path — see set_invitation_probe()'s docstring
            # (design decision #9, migration 0002) for why this call is
            # required, not optional.
            await set_invitation_probe(session, str(invitation_id))

            probe = await session.execute(
                select(Invitation.company_id).where(Invitation.id == invitation_id)
            )
            company_id = probe.scalar_one_or_none()
            if company_id is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found")

            await set_current_tenant(session, str(company_id))

            # FOR UPDATE: two concurrent accepts of the same invitation would
            # otherwise both pass the accepted_at check below under READ
            # COMMITTED and race to insert a User — harmless today only
            # because users.email's unique constraint happens to arbitrate
            # it, at the cost of a misleading "Email already registered" for
            # the loser. Locking the row makes the guarantee by design.
            result = await session.execute(
                select(Invitation).where(Invitation.id == invitation_id).with_for_update()
            )
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


# --- Outstanding-invitation management --------------------------------------
#
# `POST /invitations` and `POST /invitations/{id}/accept` were the only two
# routes here, so an admin could send an invitation and then never see it
# again: no way to check what was outstanding, and no way to revoke one sent
# to the wrong address. Since the invitation id IS the credential that
# accepts it, "sent to the wrong address" is a live grant to a stranger's
# mailbox, revocable only by editing the database.


@router.get("", response_model=InvitationListResponse)
async def list_invitations(
    current: CurrentUser = Depends(require_role("admin")),
    include_accepted: bool = False,
) -> InvitationListResponse:
    """Outstanding invitations for the caller's active tenant.

    Defaults to unaccepted ones only — the actionable set. `include_accepted`
    returns the full history, which is what an admin auditing "who was
    invited, by which address" wants.

    Scoped explicitly to `current.company_id` on top of RLS, for the same
    reason `list_company_members` is: a parent-branch admin can see
    descendant rows, and this list should be about the company they are
    acting as.
    """
    query = select(Invitation).where(Invitation.company_id == current.company_id)
    if not include_accepted:
        query = query.where(Invitation.accepted_at.is_(None))

    # `expires_at`, not a created_at — Invitation has no creation timestamp
    # (it composes UUIDPKMixin only). The TTL is a fixed 7 days, so newest
    # expiry is newest invitation; `id` breaks ties from the same instant.
    result = await current.session.execute(
        query.order_by(Invitation.expires_at.desc(), Invitation.id)
    )
    return InvitationListResponse(
        items=[InvitationResponse.model_validate(row) for row in result.scalars().all()]
    )


@router.delete("/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    invitation_id: uuid.UUID,
    current: CurrentUser = Depends(require_role("admin")),
    _ro: None = Depends(block_if_read_only),
) -> None:
    """Revoke an unaccepted invitation.

    Deleted, not flagged: the row's mere existence is what
    `POST /invitations/{id}/accept` checks, so removing it is what actually
    withdraws the grant. An already-accepted invitation is a 409 rather than
    a silent no-op — deleting it would not un-create the membership it
    produced, and pretending otherwise is worse than refusing.
    """
    result = await current.session.execute(
        select(Invitation).where(
            Invitation.id == invitation_id, Invitation.company_id == current.company_id
        )
    )
    invitation = result.scalar_one_or_none()
    if invitation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found")
    if invitation.accepted_at is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This invitation was already accepted; remove the member instead",
        )

    await current.session.delete(invitation)
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="invitation.revoked",
        entity_type="invitation",
        entity_id=invitation_id,
    )
