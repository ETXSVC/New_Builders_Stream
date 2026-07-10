import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

# No `DocumentUploadRequest` here, deliberately. `POST /projects/{id}/documents`
# (Task 1.15) accepts `multipart/form-data`, not a JSON body — the request is
# a FastAPI `UploadFile` plus a `file_name` form field, both declared directly
# as router function parameters (`file: UploadFile`, `file_name: str = Form(...)`),
# not modeled as a Pydantic request schema. Pydantic's `BaseModel` validates
# JSON request bodies; multipart form parts are FastAPI's own `Form`/`File`
# machinery. Only the *response* shape (below) and the list envelope are
# meaningfully "schemas" here.


class DocumentResponse(BaseModel):
    """Full model. `documents` has `created_at` but no `updated_at`
    (docs/04-database-schema.md Section 4) — new versions are new rows
    (`version` incremented), never an UPDATE of an existing row, so there's
    no last-modified concept to track."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    company_id: uuid.UUID
    file_name: str
    storage_path: str
    version: int
    uploaded_by: uuid.UUID
    created_at: datetime


class DocumentListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /projects/{id}/documents`
    (Task 1.15), following the exact pattern of `LeadListResponse`
    (app/schemas/lead.py). `next_cursor` is `None` once the caller has
    reached the last page. Per US-3.4 ("most recent version accessible by
    default, prior versions accessible"), the route filters `items` to the
    latest `version` per `file_name` by default — that filtering is the
    router/query's job, not this envelope's."""

    items: list[DocumentResponse]
    next_cursor: str | None = None
