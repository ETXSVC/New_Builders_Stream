import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SubcontractorAssignmentCreateRequest(BaseModel):
    """Body for `POST /projects/{id}/subcontractor-assignments` (Task 3.11).
    Per `SubcontractorAssignment`'s own docstring (Task 3.1,
    `app/models/subcontractor_assignment.py`).

    `project_id`/`company_id`/`assigned_by` are deliberately not fields
    here: the router derives `project_id` from the path, `company_id` from
    `project.company_id` (never `current.company_id` — see
    `create_subcontractor_assignment`'s own docstring,
    `app/routers/subcontractor_assignments.py`, for the full rationale), and
    `assigned_by` from `current.user.id` — same "server owns the tenant/
    actor scoping columns, schema doesn't expose them" pattern every other
    `*CreateRequest` in this codebase follows (e.g. `ChangeOrderCreateRequest`
    for `status`, `SubcontractorCreateRequest` for `company_id`).

    `override_reason` is optional at the SCHEMA layer — it's only actually
    required when the router's own business-rule check determines this
    Subcontractor has an expired compliance document AND the acting user is
    an Admin (a Project Manager can never override at all, regardless of
    what's supplied here). That conditional requirement can't be expressed
    as a Pydantic field constraint since it depends on both the caller's
    role and a DB lookup, so it's enforced in the router body instead — the
    same "business-rule check lives in the router, not the schema/a role
    gate" pattern `update_project_status`'s Change-Order-block check
    (`app/routers/projects.py`, Task 2.23) establishes.
    """

    subcontractor_id: uuid.UUID
    override_reason: str | None = None


class SubcontractorAssignmentResponse(BaseModel):
    """Full model. No `updated_at` field — `SubcontractorAssignment`
    (Task 3.1) has no such column at all (`TimestampMixin` only, no
    `UpdatedAtMixin`), matching `ChangeOrder`'s/`Phase`'s own precedent for
    a row that's never PATCH-able after creation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    subcontractor_id: uuid.UUID
    company_id: uuid.UUID
    assigned_by: uuid.UUID
    override_reason: str | None
    created_at: datetime


class SubcontractorAssignmentListResponse(BaseModel):
    """Cursor-paginated list envelope for
    `GET /projects/{id}/subcontractor-assignments` (Task 3.11), following
    the exact pattern of `ChangeOrderListResponse`
    (`app/schemas/change_order.py`). `next_cursor` is `None` once the
    caller has reached the last page."""

    items: list[SubcontractorAssignmentResponse]
    next_cursor: str | None = None
