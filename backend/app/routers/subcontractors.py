"""Task 3.4: `POST/GET /subcontractors`, `GET /subcontractors/{id}`.

`Subcontractor` is a standalone, company-scoped resource with no project/
lead parent — closer in shape to `MarkupProfile`/`CostCatalogItem`
(app/routers/catalogs.py) than to any project-nested resource like Change
Orders/Documents. Unlike `MarkupProfile` (no usable timestamp column) and
`CostCatalogItem` (needs in-memory inheritance resolution), `Subcontractor`
has a plain `created_at` column (`TimestampMixin`, `app/models/subcontractor.py`)
and no inheritance concept at all, so its list route uses the STANDARD
`app.core.pagination.paginate()` helper, the same one `list_estimates`/
`list_projects`/`list_leads` use — not `catalogs.py`'s bespoke in-memory/
id-only pagination helpers, which exist specifically to work around problems
`Subcontractor` doesn't have.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import Subcontractor
from app.schemas.subcontractor import (
    SubcontractorCreateRequest,
    SubcontractorListResponse,
    SubcontractorResponse,
)

router = APIRouter(prefix="/subcontractors", tags=["subcontractors"])

# docs/07-security-compliance.md Section 2's RBAC matrix, Compliance row:
# Admin = Full CRUD, Project Manager = Read + assign only (no create),
# Accountant = Read. Field Crew and Client have no documented grant on this
# row at all, so both are absent from both tuples below.
_WRITE_ROLES = ("admin",)
_READ_ROLES = ("admin", "project_manager", "accountant")


async def _get_subcontractor_or_404(current: CurrentUser, subcontractor_id: uuid.UUID) -> Subcontractor:
    """Shared existence/tenant check, same pattern as `_get_estimate_or_404`
    (app/routers/estimates.py) — RLS makes another tenant's subcontractor
    invisible, so this 404 covers both "doesn't exist" and "exists but isn't
    yours" identically (Inherited Invariant #8), intentionally
    indistinguishable from outside. No explicit `company_id` filter in the
    query below — the tenant_isolation RLS policy already does that scoping."""
    result = await current.session.execute(
        select(Subcontractor).where(Subcontractor.id == subcontractor_id)
    )
    subcontractor = result.scalar_one_or_none()
    if subcontractor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subcontractor not found")
    return subcontractor


@router.post("", response_model=SubcontractorResponse, status_code=status.HTTP_201_CREATED)
async def create_subcontractor(
    payload: SubcontractorCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
) -> SubcontractorResponse:
    """`company_id=current.company_id` directly — a standalone top-level
    resource with no parent entity's own `company_id` to defer to, matching
    `create_project`'s/`create_lead`'s own precedent (not the nested-resource
    pattern used for tables that derive `company_id` from a parent entity,
    e.g. `EstimateLineItem.company_id` in `replace_estimate_line_items`)."""
    subcontractor = Subcontractor(
        company_id=current.company_id,
        name=payload.name,
        trade=payload.trade,
        contact_email=payload.contact_email,
    )
    current.session.add(subcontractor)
    await current.session.flush()
    # No audit_log entry: Subcontractor creation isn't in
    # docs/07-security-compliance.md Section 5's enumerated list of
    # financially/legally significant state changes needing an audit trail
    # — same "not every create needs an audit entry" precedent
    # create_catalog_item/create_markup_profile already establish
    # (app/routers/catalogs.py). No explicit commit either — get_current_user
    # (Inherited Invariant #4) commits current.session once, after this
    # handler returns.

    return SubcontractorResponse.model_validate(subcontractor)


@router.get("", response_model=SubcontractorListResponse)
async def list_subcontractors(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> SubcontractorListResponse:
    """Plain company-scoped list, no status/role-based row-scoping —
    unlike Estimates' `client` status-scoping, there is no `client`-role
    access to this resource at all (`client` is absent from `_READ_ROLES`).
    No explicit `company_id` filter needed: the tenant_isolation RLS policy
    already scopes every row this query can see to the caller's active
    tenant, same pattern `list_estimates`/`list_projects`/`list_leads` rely
    on."""
    query = select(Subcontractor)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Subcontractor.created_at,
        id_col=Subcontractor.id,
        cursor=cursor,
        limit=limit,
    )

    return SubcontractorListResponse(
        items=[SubcontractorResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/{subcontractor_id}", response_model=SubcontractorResponse)
async def get_subcontractor(
    subcontractor_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> SubcontractorResponse:
    subcontractor = await _get_subcontractor_or_404(current, subcontractor_id)
    return SubcontractorResponse.model_validate(subcontractor)
