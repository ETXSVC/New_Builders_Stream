"""Row-level scoping for the `client` role — the one place the rule lives.

Background (migration 0019's docstring has the full writeup): RLS is
company-scoped, so it stops tenant A from seeing tenant B. It has nothing
to say about two customers *of the same company*, and until 0019 the
client-facing routes narrowed by document status only — `status='sent'`
estimates, `status='pending'` change orders, non-draft invoices. Status is
not identity: every client of a company could read every other client's
pricing, margins, invoices and signed contracts, and could legally e-sign
a contract that was never theirs.

`project_clients` / `lead_clients` supply the missing edge. This module
turns that edge into the two things callers need:

  * `client_project_scope()` / `client_lead_scope()` — SQLAlchemy
    conditions to AND into a list query;
  * `require_client_access_to_*()` — a 404 guard for by-id routes.

Everything raises **404**, never 403, matching this codebase's Inherited
Invariant #8: "doesn't exist" and "exists but isn't yours" are
deliberately indistinguishable from outside, so a client can't enumerate
another client's document ids by probing status codes.

Non-client roles are never scoped here. `require_role(...)` at the route
already decides which staff roles may read a surface at all, and staff are
company-wide readers by design — the guards below are no-ops for them, so
a caller can apply them unconditionally without an `if role == "client"`
at every site.
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser
from app.models import Estimate, LeadClient, ProjectClient

CLIENT_ROLE = "client"

_NOT_FOUND = "not found"


def _membership_exists(model, parent_column, parent_value, user_id: uuid.UUID):
    """`EXISTS (SELECT 1 FROM <membership> WHERE parent = ? AND user = ?)`.

    Kept as a correlated EXISTS rather than an `IN (SELECT ...)`: the
    parent column is often another table's column (see
    `client_project_scope`), and EXISTS lets Postgres stop at the first
    matching membership row instead of materializing every project the
    caller belongs to.
    """
    return exists(
        select(1).where(parent_column == parent_value, model.user_id == user_id)
    )


def client_project_scope(current: CurrentUser, project_id_column):
    """Condition restricting rows to projects this client is a member of.

    `project_id_column` is the *column* carrying the project id on whatever
    is being listed (`Invoice.project_id`, `ChangeOrder.project_id`, ...),
    so this composes into a query without a join.

    Returns `True` (a no-op condition) for non-client roles — see the module
    docstring.
    """
    if current.role != CLIENT_ROLE:
        return True
    return _membership_exists(
        ProjectClient, ProjectClient.project_id, project_id_column, current.user.id
    )


def client_estimate_scope(current: CurrentUser):
    """Condition restricting Estimates to ones this client may see.

    An Estimate hangs off either a Project or a bare Lead — `project_id` is
    nullable — so both memberships count. A lead-backed estimate is exactly
    the new-business case where the prospective customer has no project
    yet, and dropping it would break the flow the client role exists for.
    """
    if current.role != CLIENT_ROLE:
        return True
    return or_(
        _membership_exists(
            ProjectClient, ProjectClient.project_id, Estimate.project_id, current.user.id
        ),
        _membership_exists(LeadClient, LeadClient.lead_id, Estimate.lead_id, current.user.id),
    )


async def _has_project_membership(
    session: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    result = await session.execute(
        select(ProjectClient.id).where(
            ProjectClient.project_id == project_id, ProjectClient.user_id == user_id
        )
    )
    return result.first() is not None


async def _has_lead_membership(
    session: AsyncSession, *, lead_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    result = await session.execute(
        select(LeadClient.id).where(
            LeadClient.lead_id == lead_id, LeadClient.user_id == user_id
        )
    )
    return result.first() is not None


def require_signer_is_caller(current: CurrentUser, signer_email: str) -> None:
    """422 unless the submitted `signer_email` is the caller's own.

    `signer_name`/`signer_email` arrive as free-text `Form(...)` fields on
    the approve routes and were never compared to anything — so the
    attribution on a record whose entire purpose is legal evidence of
    contract acceptance was whatever the caller typed. Combined with
    `signed_by_user_id`, this makes the pair verifiable: the typed block is
    the signature as it appears on the contract, the FK is the account, and
    this check is the assertion that they describe the same person.

    Only `signer_email` is checked. `signer_name` stays free text on
    purpose — people legitimately sign as "Bob Smith", "Robert J. Smith",
    or a spouse's name on a joint contract, and rejecting those would be
    wrong; the account FK is what carries the identity claim.

    Case- and whitespace-insensitive, because an email is: a client who
    types `Bob@Example.com` for an account registered as `bob@example.com`
    is the same person, and failing them at the signature step would be a
    dead end with no way forward.

    422 rather than 403: this is a malformed submission the caller can fix
    by correcting a field, not an authorization decision about the
    document (that already happened in `_get_estimate_or_404`).
    """
    if current.role != CLIENT_ROLE:
        return
    if signer_email.strip().casefold() != current.user.email.strip().casefold():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "signer_email must match the signed-in account's email address",
        )


async def require_client_access_to_project(
    current: CurrentUser, project_id: uuid.UUID, *, entity: str = "Project"
) -> None:
    """404 unless this client is a member of `project_id`. No-op for staff."""
    if current.role != CLIENT_ROLE:
        return
    if not await _has_project_membership(
        current.session, project_id=project_id, user_id=current.user.id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{entity} {_NOT_FOUND}")


async def require_client_access_to_estimate(
    current: CurrentUser, estimate: Estimate, *, entity: str = "Estimate"
) -> None:
    """404 unless this client is a member of the estimate's project or lead.

    This is the guard `POST /estimates/{id}/approve` was missing entirely —
    it was gated on `require_role("client")` plus a status check, so any
    client of the company could e-sign any other client's contract.
    """
    if current.role != CLIENT_ROLE:
        return

    if estimate.project_id is not None and await _has_project_membership(
        current.session, project_id=estimate.project_id, user_id=current.user.id
    ):
        return
    if estimate.lead_id is not None and await _has_lead_membership(
        current.session, lead_id=estimate.lead_id, user_id=current.user.id
    ):
        return

    raise HTTPException(status.HTTP_404_NOT_FOUND, f"{entity} {_NOT_FOUND}")
