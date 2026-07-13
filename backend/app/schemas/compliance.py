import uuid
from datetime import date

from pydantic import BaseModel

# Deliberately NOT `ConfigDict(from_attributes=True)`: unlike
# `ComplianceDocumentResponse` (app/schemas/compliance_document.py), which
# maps 1:1 onto a single ORM row, `ComplianceDashboardEntry` is assembled in
# `app/routers/compliance.py` from a joined `(ComplianceDocument,
# Subcontractor)` row pair plus a `status` field that exists on neither ORM
# model (it's computed in Python by comparing `expires_on` to today). There
# is no single ORM object `.model_validate()` could point at, so every
# instance is built with plain keyword construction instead.


class ComplianceDashboardEntry(BaseModel):
    """One expiring-or-expired compliance document, joined with its owning
    subcontractor's display fields. `status` is computed in the router, not
    stored: `"expiring_soon"` if `today <= expires_on <= today + 30 days`,
    `"expired"` if `expires_on < today`."""

    compliance_document_id: uuid.UUID
    subcontractor_id: uuid.UUID
    subcontractor_name: str
    doc_type: str
    expires_on: date
    status: str


class ComplianceDashboardResponse(BaseModel):
    """No `next_cursor`/pagination envelope, unlike
    `ComplianceDocumentListResponse` — Task 3.6 deliberately omits
    pagination for this route (a company-wide compliance dashboard is
    expected to be a bounded, glanceable list)."""

    items: list[ComplianceDashboardEntry]
