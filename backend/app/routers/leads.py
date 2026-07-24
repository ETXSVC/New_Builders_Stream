import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.events import publish
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import CommunicationLog, Lead
from app.models.lead import VALID_STATUSES
from app.schemas.communication_log import (
    CommunicationLogCreateRequest,
    CommunicationLogListResponse,
    CommunicationLogResponse,
)
from app.schemas.lead import LeadCreateRequest, LeadListResponse, LeadResponse, LeadUpdateRequest
from app.services.audit import write_audit_log
from app.services.lead_transitions import is_legal_transition

router = APIRouter(prefix="/leads", tags=["leads"])

# CRM = Admin + PM full CRUD, nobody else (docs/07-security-compliance.md Section 2's
# RBAC matrix). Same two roles for create/list/get — Task 1.5's PATCH reuses this too.
_LEAD_ROLES = ("admin", "project_manager")


async def _get_lead_or_404(current: CurrentUser, lead_id: uuid.UUID) -> Lead:
    """Shared existence/tenant check reused by every route below that
    operates on a single lead (by path lead_id) or a resource nested under
    one. RLS makes another tenant's lead invisible, so this 404 covers both
    "doesn't exist" and "exists but isn't yours" — intentionally
    indistinguishable from outside, same pattern as GET /companies/{id}.
    For the nested communications routes, this MUST be called before any
    communication_logs read/write, so a caller can never create or list
    comm logs under a lead_id they couldn't otherwise see via GET
    /leads/{id}."""
    result = await current.session.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if lead is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return lead


@router.post("", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreateRequest,
    current: CurrentUser = Depends(require_role(*_LEAD_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> LeadResponse:
    lead = Lead(
        company_id=current.company_id,
        contact_name=payload.contact_name,
        project_name=payload.project_name,
        email=payload.email,
        phone=payload.phone,
        project_type=payload.project_type,
        estimated_value=payload.estimated_value,
        notes=payload.notes,
        status="new",
    )
    current.session.add(lead)
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="lead.created",
        entity_type="lead",
        entity_id=lead.id,
    )
    # No explicit commit here — get_current_user (design decision #8) commits
    # current.session once, after this handler returns.

    return LeadResponse.model_validate(lead)


@router.get("", response_model=LeadListResponse)
async def list_leads(
    current: CurrentUser = Depends(require_role(*_LEAD_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> LeadListResponse:
    if status_filter is not None and status_filter not in VALID_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"status must be one of {VALID_STATUSES}"
        )

    # No explicit company_id filter here — same pattern as GET /companies/{id}:
    # the tenant_isolation RLS policy (0003 migration) already scopes every
    # row this query can see to the caller's active tenant (and descendants).
    query = select(Lead)
    if status_filter is not None:
        query = query.where(Lead.status == status_filter)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Lead.created_at,
        id_col=Lead.id,
        cursor=cursor,
        limit=limit,
    )

    return LeadListResponse(
        items=[LeadResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/{lead_id}", response_model=LeadResponse)
async def get_lead(
    lead_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_LEAD_ROLES)),
) -> LeadResponse:
    lead = await _get_lead_or_404(current, lead_id)
    return LeadResponse.model_validate(lead)


@router.patch("/{lead_id}", response_model=LeadResponse)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdateRequest,
    current: CurrentUser = Depends(require_role(*_LEAD_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> LeadResponse:
    lead = await _get_lead_or_404(current, lead_id)

    previous_status = lead.status
    requested_status = payload.status
    # A resubmission of the lead's current status is treated as a no-op, not
    # a transition — it isn't modeled in lead_transitions.LEAD_TRANSITIONS
    # (no self-loops) and shouldn't 409 just because the caller PATCHed a
    # field alongside an unchanged status.
    status_changing = requested_status is not None and requested_status != previous_status

    # Validate the transition BEFORE touching the ORM object at all (Task
    # 1.5: "don't let a request patch both a valid field and an illegal
    # status transition and have one silently fail while the other succeeds
    # — should be one transaction, one outcome"; Inherited Invariant #4: this
    # handler reuses current.session and never commits inline, so raising
    # here — before any setattr — guarantees nothing from this request is
    # staged for the eventual single commit that get_current_user performs).
    # The redundant-looking `requested_status is not None` re-checks here
    # and below are for the type checker: `status_changing` already implies
    # it, but mypy can't carry that narrowing through the boolean alias.
    if (
        status_changing
        and requested_status is not None
        and not is_legal_transition(previous_status, requested_status)
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Illegal lead status transition: {previous_status} -> {requested_status}",
        )

    update_fields = payload.model_dump(exclude_unset=True, exclude={"status"})
    for field_name, value in update_fields.items():
        setattr(lead, field_name, value)
    if status_changing and requested_status is not None:
        lead.status = requested_status

    # updated_at bumps automatically via UpdatedAtMixin's onupdate=utcnow the
    # moment any of the setattr()s above (or the status assignment) makes
    # this row dirty and it gets flushed — no manual touch needed.
    await current.session.flush()

    if status_changing:
        await write_audit_log(
            current.session,
            company_id=current.company_id,
            actor_id=current.user.id,
            action="lead.status_changed",
            entity_type="lead",
            entity_id=lead.id,
            metadata={"from": previous_status, "to": requested_status},
        )

        # Task 1.18 registered a real handler (app.services.lead_won_handler)
        # against this event name, so this is no longer a no-op dispatch —
        # see app/core/event_handlers.py for the registration call, and
        # handle_lead_won's own docstring for why `session` and
        # `company_id=lead.company_id` (not `current.company_id`) are passed
        # the way they are below.
        if requested_status == "won":
            await publish(
                "LEAD_WON",
                session=current.session,
                lead_id=lead.id,
                company_id=lead.company_id,
                contact_name=lead.contact_name,
                project_name=lead.project_name,
                actor_id=current.user.id,
            )

    return LeadResponse.model_validate(lead)


@router.post(
    "/{lead_id}/communications",
    response_model=CommunicationLogResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_communication_log(
    lead_id: uuid.UUID,
    payload: CommunicationLogCreateRequest,
    current: CurrentUser = Depends(require_role(*_LEAD_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> CommunicationLogResponse:
    # Must stay first — see _get_lead_or_404's docstring for why moving this
    # below the CommunicationLog insert would be an information-disclosure bug.
    await _get_lead_or_404(current, lead_id)

    log = CommunicationLog(
        lead_id=lead_id,
        company_id=current.company_id,
        author_id=current.user.id,
        channel=payload.channel,
        body=payload.body,
    )
    current.session.add(log)
    await current.session.flush()
    # No explicit commit here — get_current_user (design decision #8) commits
    # current.session once, after this handler returns. No audit_log entry
    # either: unlike Company/Invitation/Lead (which get audited on create),
    # a CommunicationLog carries its own author_id and created_at directly
    # on the row, and Task 1.2's REVOKE UPDATE/DELETE guarantees that pairing
    # can never be altered — the row IS the permanent who/when record, so a
    # parallel audit_log entry would only duplicate information the row
    # already carries immutably, not add anything a "who created this and
    # when" investigation couldn't already get from the row itself.

    return CommunicationLogResponse.model_validate(log)


@router.get("/{lead_id}/communications", response_model=CommunicationLogListResponse)
async def list_communication_logs(
    lead_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_LEAD_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> CommunicationLogListResponse:
    # Must stay first — see _get_lead_or_404's docstring for why moving this
    # below the CommunicationLog query would be an information-disclosure bug.
    await _get_lead_or_404(current, lead_id)

    # Oldest-first (US-2.4: "see a chronological history") — paginate()'s
    # ORDER BY created_at ASC, id ASC is exactly this reading order, so no
    # extra ordering argument is needed here beyond the composite tiebreaker
    # paginate() already applies for every list endpoint.
    query = select(CommunicationLog).where(CommunicationLog.lead_id == lead_id)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=CommunicationLog.created_at,
        id_col=CommunicationLog.id,
        cursor=cursor,
        limit=limit,
    )

    return CommunicationLogListResponse(
        items=[CommunicationLogResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )
