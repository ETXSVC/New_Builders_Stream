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
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import ComplianceDocument, Subcontractor
from app.models.compliance_document import VALID_DOC_TYPES
from app.schemas.compliance_document import (
    ComplianceDocumentListResponse,
    ComplianceDocumentResponse,
)
from app.schemas.subcontractor import (
    SubcontractorCreateRequest,
    SubcontractorListResponse,
    SubcontractorResponse,
)
from app.services.document_storage import InvalidFileNameError, write_compliance_document_file

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
    _ro: None = Depends(block_if_read_only),
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


@router.post(
    "/{subcontractor_id}/compliance-documents",
    response_model=ComplianceDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_compliance_document(
    subcontractor_id: uuid.UUID,
    doc_type: str = Form(...),
    expires_on: date = Form(...),
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> ComplianceDocumentResponse:
    """Task 3.5. `require_role("admin")` only (`_WRITE_ROLES`) — same RBAC
    row this router's module docstring already cites: Compliance is "Full
    CRUD" for Admin only.

    `_get_subcontractor_or_404` first, same ordering `upload_document`
    (app/routers/projects.py) uses: existence/tenant check before any
    semantic validation of the payload, so a cross-tenant/nonexistent
    `subcontractor_id` always 404s before the caller learns anything about
    whether `doc_type` was valid.

    `doc_type` is validated against `VALID_DOC_TYPES`
    (`app/models/compliance_document.py`) in Python, BEFORE the uploaded
    file is ever written to disk — mirrors `capture_esignature`'s own Task
    2.18 "validate before any side effect" fix
    (`app/services/esignature.py`'s `document_type not in
    VALID_DOCUMENT_TYPES` check): without this, a rejected `doc_type` would
    still leave behind a real, orphaned file on disk with no corresponding
    row. Raised as `HTTPException(422, ...)` rather than `ValueError`
    because this is a router (not a low-level shared service function),
    same 422 mapping `validate_file_name`'s `InvalidFileNameError` gets in
    `upload_document`.

    **ID-generation sequencing**: follows `capture_esignature`'s exact
    pattern — `compliance_document_id = uuid.uuid4()` is generated
    explicitly here BEFORE the file is written, that same id is passed into
    `write_compliance_document_file(...)`, and the same id is then passed
    explicitly into `ComplianceDocument(id=compliance_document_id, ...)`.
    Deliberately NOT `upload_document`'s flush-then-read-the-id ordering
    (that pattern works there because `write_document_file` doesn't need
    the row's own id as a path component at all — `write_compliance_document_file`
    does, since the compliance document's id is itself part of the storage
    path).
    """
    subcontractor = await _get_subcontractor_or_404(current, subcontractor_id)

    if doc_type not in VALID_DOC_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"doc_type must be one of {VALID_DOC_TYPES}, got {doc_type!r}",
        )

    compliance_document_id = uuid.uuid4()
    content = await file.read()

    try:
        storage_path = write_compliance_document_file(
            company_id=subcontractor.company_id,
            subcontractor_id=subcontractor.id,
            compliance_document_id=compliance_document_id,
            original_filename=file.filename or "",
            content=content,
        )
    except InvalidFileNameError as exc:
        # 422, not 400/403/500: `write_compliance_document_file`'s own
        # `_validate_extension` rejects a control-character-containing or
        # oversized extension outright (see that function's docstring for
        # the two concrete unhandled-500 cases this closes) — same
        # `InvalidFileNameError` -> 422 mapping `upload_document`
        # (app/routers/projects.py) uses for `validate_file_name`. Raised
        # AFTER `content = await file.read()` but BEFORE any bytes reach
        # disk, so an invalid extension never leaves an orphaned file.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    compliance_document = ComplianceDocument(
        id=compliance_document_id,
        subcontractor_id=subcontractor.id,
        company_id=subcontractor.company_id,
        doc_type=doc_type,
        storage_path=storage_path,
        expires_on=expires_on,
    )
    current.session.add(compliance_document)
    await current.session.flush()
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns. No audit_log entry
    # either: compliance document upload isn't in
    # docs/07-security-compliance.md Section 5's enumerated list of
    # state changes needing an audit trail (that list is status
    # transitions/approvals), same "not every create needs an audit entry"
    # precedent create_subcontractor above already establishes.

    return ComplianceDocumentResponse.model_validate(compliance_document)


@router.get(
    "/{subcontractor_id}/compliance-documents",
    response_model=ComplianceDocumentListResponse,
)
async def list_compliance_documents(
    subcontractor_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ComplianceDocumentListResponse:
    """Task 3.5. `_get_subcontractor_or_404` first — a nonexistent/
    cross-tenant `subcontractor_id` in the path must 404, not just return an
    empty list (the RLS `compliance_documents` scan alone would silently
    return zero rows for a cross-tenant id, which would be indistinguishable
    from "this subcontractor genuinely has no compliance documents yet" —
    the explicit existence check up front avoids that ambiguity, same
    reasoning `list_documents`/other project-nested list routes already
    apply via their own `_get_project_or_404` call).

    Standard `paginate()` helper, `created_at_col=ComplianceDocument.created_at,
    id_col=ComplianceDocument.id`, same as `list_subcontractors` above — this
    table has a plain `created_at` column (`TimestampMixin`) and no
    in-memory/inheritance-resolution complications, so there's no reason to
    reach for `catalogs.py`'s bespoke pagination helpers.
    """
    subcontractor = await _get_subcontractor_or_404(current, subcontractor_id)

    query = select(ComplianceDocument).where(ComplianceDocument.subcontractor_id == subcontractor.id)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=ComplianceDocument.created_at,
        id_col=ComplianceDocument.id,
        cursor=cursor,
        limit=limit,
    )

    return ComplianceDocumentListResponse(
        items=[ComplianceDocumentResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )
