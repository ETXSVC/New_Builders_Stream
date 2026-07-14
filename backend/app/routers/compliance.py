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

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import ComplianceDocument, ComplianceNotification, Subcontractor
from app.schemas.compliance import (
    ComplianceDashboardEntry,
    ComplianceDashboardResponse,
    ComplianceNotificationEntry,
    ComplianceNotificationListResponse,
)

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

# Task 3.10. Per the design spec (US-7.1: notifications go "to the Admin"
# specifically) — deliberately NOT `_READ_ROLES` above. This is the one
# Compliance route where `project_manager`/`accountant` are excluded, unlike
# every other read route in this feature. Its own tuple, not a reuse of
# `_READ_ROLES`, for the same "two routers'/routes' RBAC shouldn't drift
# silently if one changes without the other" reasoning `_READ_ROLES`'s own
# comment above already gives.
_NOTIFICATION_ROLES = ("admin",)

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


async def _get_notification_or_404(
    current: CurrentUser, notification_id: uuid.UUID
) -> ComplianceNotification:
    """Shared existence/tenant check, same pattern as
    `_get_subcontractor_or_404` (app/routers/subcontractors.py) — RLS makes
    another tenant's notification invisible, so this 404 covers both
    "doesn't exist" and "exists but isn't yours" identically (Inherited
    Invariant #8). No explicit `company_id` filter in the query below — the
    tenant_isolation RLS policy already does that scoping."""
    result = await current.session.execute(
        select(ComplianceNotification).where(ComplianceNotification.id == notification_id)
    )
    notification = result.scalar_one_or_none()
    if notification is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Compliance notification not found")
    return notification


@router.get("/notifications", response_model=ComplianceNotificationListResponse)
async def list_compliance_notifications(
    current: CurrentUser = Depends(require_role(*_NOTIFICATION_ROLES)),
    unread_only: bool = Query(False),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ComplianceNotificationListResponse:
    """Task 3.10. `require_role("admin")` only (`_NOTIFICATION_ROLES`), not
    this router's `_READ_ROLES` — see that tuple's own comment above.

    Paginated via the standard `paginate()` helper
    (`created_at_col=ComplianceNotification.fired_at,
    id_col=ComplianceNotification.id`, since this table has no `created_at`
    of its own — `fired_at` plays that role, same rationale
    `ComplianceNotification`'s own model docstring gives), scoped to a plain
    `select(ComplianceNotification)` — no join in the paginated query itself,
    since `paginate()`'s `result.scalars().all()` only works against a
    single-entity `select()` (unlike the dashboard route's own manual
    `select(ComplianceDocument, Subcontractor)` two-entity join, which
    doesn't go through `paginate()` at all). The `compliance_documents`/
    `subcontractors` display-context join happens in a SEPARATE, second
    query below, scoped to just the `compliance_document_id`s present on
    this page — the page is already bounded by `limit`, so this is a bounded
    second round-trip, not an N+1.

    `?unread_only=true` filters to `read_at IS NULL` before pagination is
    applied, so the cursor/limit math is against the already-filtered set.

    No explicit `company_id` filter anywhere: the tenant_isolation RLS
    policy on `compliance_notifications` (and the two joined tables) already
    scopes every row this query can see to the caller's active tenant, same
    pattern every other router in this codebase relies on.
    """
    query = select(ComplianceNotification)
    if unread_only:
        query = query.where(ComplianceNotification.read_at.is_(None))

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=ComplianceNotification.fired_at,
        id_col=ComplianceNotification.id,
        cursor=cursor,
        limit=limit,
    )

    if not rows:
        return ComplianceNotificationListResponse(items=[], next_cursor=next_cursor)

    document_ids = [row.compliance_document_id for row in rows]
    join_query = (
        select(ComplianceDocument, Subcontractor)
        .join(Subcontractor, ComplianceDocument.subcontractor_id == Subcontractor.id)
        .where(ComplianceDocument.id.in_(document_ids))
    )
    join_result = await current.session.execute(join_query)
    context_by_document_id = {
        document.id: (document, subcontractor) for document, subcontractor in join_result.all()
    }

    items = []
    for notification in rows:
        document, subcontractor = context_by_document_id[notification.compliance_document_id]
        items.append(
            ComplianceNotificationEntry(
                id=notification.id,
                compliance_document_id=document.id,
                subcontractor_name=subcontractor.name,
                doc_type=document.doc_type,
                expires_on=document.expires_on,
                threshold=notification.threshold,
                fired_at=notification.fired_at,
                read_at=notification.read_at,
            )
        )

    return ComplianceNotificationListResponse(items=items, next_cursor=next_cursor)


@router.post("/notifications/{notification_id}/dismiss", response_model=ComplianceNotificationEntry)
async def dismiss_compliance_notification(
    notification_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_NOTIFICATION_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> ComplianceNotificationEntry:
    """Task 3.10. `require_role("admin")` only, same `_NOTIFICATION_ROLES`
    tuple the list route above uses.

    Idempotent: sets `read_at = datetime.now(timezone.utc)` only if it's
    currently `None`. Dismissing an already-dismissed notification is a 200
    no-op — `read_at` is NOT bumped to a later timestamp on a repeat call —
    rather than a 409, per the task spec.

    `datetime.now(timezone.utc)`, not the model-layer `utcnow` helper
    (`app/models/base.py`): that helper is used as a column `default=`/
    `onupdate=` inside model DEFINITIONS (e.g.
    `app/models/compliance_notification.py`'s own `fired_at` column), not
    from router code setting a value explicitly — router code elsewhere in
    this codebase (`app/routers/invitations.py`'s `accepted_at`,
    `app/services/esignature.py`'s `signed_at`) always calls
    `datetime.now(timezone.utc)` directly instead.
    """
    notification = await _get_notification_or_404(current, notification_id)

    if notification.read_at is None:
        notification.read_at = datetime.now(timezone.utc)
        await current.session.flush()
        # No explicit commit — get_current_user (Inherited Invariant #4)
        # commits current.session once, after this handler returns. No
        # audit_log entry either: dismissing a notification isn't in
        # docs/07-security-compliance.md Section 5's enumerated list of
        # financially/legally significant state changes needing an audit
        # trail, same "not every mutation needs an audit entry" precedent
        # `create_subcontractor`/`upload_compliance_document`
        # (app/routers/subcontractors.py) already establish.

    document_query = (
        select(ComplianceDocument, Subcontractor)
        .join(Subcontractor, ComplianceDocument.subcontractor_id == Subcontractor.id)
        .where(ComplianceDocument.id == notification.compliance_document_id)
    )
    result = await current.session.execute(document_query)
    # .one() (not .one_or_none()): a notification can never outlive its
    # document — compliance_notifications.compliance_document_id has
    # ondelete="CASCADE" (Task 3.1), and compliance_documents has UPDATE/
    # DELETE revoked from app_user with no delete route anywhere in this
    # codebase (migration 0009) — so a row reachable via _get_notification_or_404
    # always has a real, joinable document. If that invariant is ever
    # relaxed (a future delete route added to compliance_documents), this
    # call needs to become .one_or_none() with a real error path.
    document, subcontractor = result.one()

    return ComplianceNotificationEntry(
        id=notification.id,
        compliance_document_id=document.id,
        subcontractor_name=subcontractor.name,
        doc_type=document.doc_type,
        expires_on=document.expires_on,
        threshold=notification.threshold,
        fired_at=notification.fired_at,
        read_at=notification.read_at,
    )
