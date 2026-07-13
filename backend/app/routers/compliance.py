"""Task 3.6: `GET /compliance/dashboard`.

This new router file is required because `Compliance` spans multiple
entities — `Subcontractor`, `ComplianceDocument`, and a future
`ComplianceNotification` — none of which it "nests under" the way
project-scoped resources do. It doesn't belong in `subcontractors.py`:
that router is organized around the `Subcontractor` entity itself (and the
compliance documents nested under a single subcontractor), whereas this
route is a cross-cutting, company-wide view spanning every subcontractor's
documents at once. Matches `esignatures.py`'s own precedent (see that
router's module docstring) of a new router file organized around a
cross-cutting concept, not a single parent entity.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.deps import CurrentUser, require_role
from app.models import ComplianceDocument, Subcontractor
from app.schemas.compliance import ComplianceDashboardEntry, ComplianceDashboardResponse

router = APIRouter(prefix="/compliance", tags=["compliance"])

# docs/07-security-compliance.md Section 2's RBAC matrix, Compliance row:
# Admin/Project Manager/Accountant all get read access (Admin = Full CRUD,
# Project Manager = Read + assign only, Accountant = Read). Field Crew and
# Client have no documented grant on this row, so both are absent. This is
# its OWN tuple, not `subcontractors.py`'s `_READ_ROLES` — this router has
# no write routes of its own, so there's no matching `_WRITE_ROLES` here at
# all, and reusing another module's role tuple across files would make the
# two routers' RBAC drift silently if one changed without the other.
_READ_ROLES = ("admin", "project_manager", "accountant")

# Per the task spec: "expiring soon" covers today through 30 days out,
# inclusive of both ends.
_EXPIRING_SOON_WINDOW = timedelta(days=30)


@router.get("/dashboard", response_model=ComplianceDashboardResponse)
async def get_compliance_dashboard(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> ComplianceDashboardResponse:
    """Company-wide expiring/expired compliance document list, computed
    live (no cached/materialized state) from `compliance_documents` joined
    to `subcontractors` for the `subcontractor_name` display field.

    A SINGLE query fetches every row where `expires_on <= today + 30 days`
    — this covers both "expiring soon" and "already expired" documents at
    once. The `status` field distinguishing the two for display is computed
    in Python per-row by comparing `expires_on` to today, not via two
    separate DB queries (one for each status) — there is no scenario where
    running this as two queries would be more correct or more efficient,
    only more code.

    No explicit `company_id` filter on either side of the join: the
    tenant_isolation RLS policy on both `compliance_documents` and
    `subcontractors` already scopes every row this query can see to the
    caller's active tenant, same pattern every other router in this
    codebase relies on (see e.g. `_get_subcontractor_or_404`,
    `app/routers/subcontractors.py`).

    No pagination: a company-wide compliance dashboard is expected to be a
    bounded, glanceable list. If this becomes a real scale problem later,
    that's a future task, not something to pre-optimize for now.
    """
    today = date.today()
    cutoff = today + _EXPIRING_SOON_WINDOW

    query = (
        select(ComplianceDocument, Subcontractor)
        .join(Subcontractor, ComplianceDocument.subcontractor_id == Subcontractor.id)
        .where(ComplianceDocument.expires_on <= cutoff)
    )
    result = await current.session.execute(query)

    items = [
        ComplianceDashboardEntry(
            compliance_document_id=document.id,
            subcontractor_id=subcontractor.id,
            subcontractor_name=subcontractor.name,
            doc_type=document.doc_type,
            expires_on=document.expires_on,
            status="expired" if document.expires_on < today else "expiring_soon",
        )
        for document, subcontractor in result.all()
    ]

    return ComplianceDashboardResponse(items=items)
