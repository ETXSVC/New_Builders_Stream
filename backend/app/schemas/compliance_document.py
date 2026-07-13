import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict

# Deliberately NO `ComplianceDocumentCreateRequest` here. The create route
# (`POST /subcontractors/{id}/compliance-documents`, a future task) is a
# `multipart/form-data` request — the file itself arrives as a separate
# `UploadFile` form field the router destructures directly, not something a
# single Pydantic body schema models. Same "no single Pydantic schema models
# a multipart body as a whole — the router takes `Form(...)`/`File(...)`
# parameters directly" convention `upload_document`
# (app/routers/projects.py, Task 1.x) already established for `Document`'s
# own multipart upload route. `doc_type` and `expires_on` will be plain
# `Form(...)` parameters in the router itself when that task is built.


class ComplianceDocumentResponse(BaseModel):
    """Full model. No `updated_at` field — `ComplianceDocument` (Task 3.1)
    has no such column at all (TimestampMixin only, no UpdatedAtMixin); no
    update route exists for this table at all, same "immutable from
    creation" precedent as `Esignature` (app/models/esignature.py)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subcontractor_id: uuid.UUID
    company_id: uuid.UUID
    doc_type: str
    storage_path: str
    expires_on: date
    created_at: datetime


class ComplianceDocumentListResponse(BaseModel):
    """Cursor-paginated list envelope for
    `GET /subcontractors/{id}/compliance-documents` (future task),
    following the exact pattern of `DailyLogListResponse`
    (app/schemas/daily_log.py). `next_cursor` is `None` once the caller has
    reached the last page."""

    items: list[ComplianceDocumentResponse]
    next_cursor: str | None = None
