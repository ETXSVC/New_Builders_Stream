import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, tuple_

from app.core.deps import CurrentUser, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursorError, decode_cursor, encode_cursor
from app.models import Lead
from app.models.lead import VALID_STATUSES
from app.schemas.lead import LeadCreateRequest, LeadListResponse, LeadResponse
from app.services.audit import write_audit_log

router = APIRouter(prefix="/leads", tags=["leads"])

# CRM = Admin + PM full CRUD, nobody else (docs/07-security-compliance.md Section 2's
# RBAC matrix). Same two roles for create/list/get — Task 1.5's PATCH reuses this too.
_LEAD_ROLES = ("admin", "project_manager")


@router.post("", response_model=LeadResponse, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreateRequest,
    current: CurrentUser = Depends(require_role(*_LEAD_ROLES)),
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

    if cursor is not None:
        try:
            cursor_created_at, cursor_id = decode_cursor(cursor)
        except InvalidCursorError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid pagination cursor")
        # Composite row comparison, not two separate AND'd conditions — see
        # app/core/pagination.py's module docstring for why (created_at, id)
        # together, compared as a tuple, is required for correctness once
        # rows can share a created_at value.
        query = query.where(tuple_(Lead.created_at, Lead.id) > (cursor_created_at, cursor_id))

    # Fetch one extra row (limit + 1) to learn whether a next page exists
    # without a second COUNT/EXISTS query; the extra row is trimmed below
    # and never returned to the caller.
    query = query.order_by(Lead.created_at.asc(), Lead.id.asc()).limit(limit + 1)

    result = await current.session.execute(query)
    rows = list(result.scalars().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return LeadListResponse(
        items=[LeadResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/{lead_id}", response_model=LeadResponse)
async def get_lead(
    lead_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_LEAD_ROLES)),
) -> LeadResponse:
    result = await current.session.execute(select(Lead).where(Lead.id == lead_id))
    lead = result.scalar_one_or_none()
    if lead is None:
        # RLS makes another tenant's lead invisible, so this 404 covers both
        # "doesn't exist" and "exists but isn't yours" — same pattern as
        # GET /companies/{id}, intentionally indistinguishable from outside.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return LeadResponse.model_validate(lead)
