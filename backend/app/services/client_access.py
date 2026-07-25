"""Granting, listing and revoking a client's access to a Project or Lead.

`app/services/client_scope.py` answers "may this client see this row?";
this module is how the answer gets set. Both membership tables have the
same shape, so both surfaces share one implementation parameterized by
table + parent column — `POST /projects/{id}/clients` and
`POST /leads/{id}/clients` differ only in which parent they name.

Writes go through here rather than through a router so the invariants hold
identically on both:

  * the target must be a **client-role member of this company** — granting
    project access to a staff account would be meaningless (staff already
    read company-wide) and granting it to a stranger's user id would be a
    cross-tenant leak;
  * the membership row is stamped with the **parent record's** company_id,
    not the acting session's — the `company_id`-stamping rule this codebase
    has had to fix seven times, applied once here instead of at two call
    sites.
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.deps import CurrentUser
from app.models import Company, CompanyUser, LeadClient, ProjectClient, User
from app.schemas.client_access import ClientAccessResponse

# (model, parent-id attribute name) for each surface. The routers pass the
# model; the attribute name is derived so a caller can't pair the wrong two.
_PARENT_COLUMN = {ProjectClient: "project_id", LeadClient: "lead_id"}


def _parent_column(model):
    return getattr(model, _PARENT_COLUMN[model])


async def _require_client_role_member(current: CurrentUser, user_id: uuid.UUID) -> User:
    """The target must hold the `client` role in the acting company.

    Looked up through `company_users` scoped to `current.company_id`, so a
    caller cannot grant access to a user who merely exists somewhere in the
    database. RLS covers the tenant tree; this narrows it to the exact
    company being acted on, and to the one role the grant is meaningful
    for.
    """
    result = await current.session.execute(
        select(User)
        .join(CompanyUser, CompanyUser.user_id == User.id)
        .where(
            User.id == user_id,
            CompanyUser.company_id == current.company_id,
            CompanyUser.role == "client",
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        # Same existence-indistinguishable 404 as everywhere else: "no such
        # user", "not in this company" and "not a client" are one answer.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client user not found")
    return user


async def grant_client_access(
    current: CurrentUser, *, model, parent_id: uuid.UUID, parent_company_id: uuid.UUID,
    user_id: uuid.UUID,
) -> ClientAccessResponse:
    user = await _require_client_role_member(current, user_id)

    membership = model(
        # The PARENT's company, not current.company_id — a parent-branch
        # admin may legitimately act on a descendant branch's project
        # without switching X-Tenant-ID, and a row stamped with the acting
        # company would be invisible under RLS to the branch that owns the
        # project it grants access to.
        company_id=parent_company_id,
        user_id=user_id,
        **{_PARENT_COLUMN[model]: parent_id},
    )
    current.session.add(membership)
    try:
        await current.session.flush()
    except IntegrityError:
        # The unique constraint on (parent, user). Re-granting is a no-op
        # the caller should know about, not a silent duplicate that a single
        # revoke would only half-remove.
        raise HTTPException(status.HTTP_409_CONFLICT, "This client already has access")

    return ClientAccessResponse(
        id=membership.id,
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        created_at=membership.created_at,
    )


async def list_client_access(
    current: CurrentUser, *, model, parent_id: uuid.UUID
) -> list[ClientAccessResponse]:
    result = await current.session.execute(
        select(model, User)
        .join(User, User.id == model.user_id)
        .where(_parent_column(model) == parent_id)
        .order_by(model.created_at.asc(), model.id.asc())
    )
    return [
        ClientAccessResponse(
            id=membership.id,
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            created_at=membership.created_at,
        )
        for membership, user in result.all()
    ]


async def revoke_client_access(
    current: CurrentUser, *, model, parent_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    result = await current.session.execute(
        select(model).where(_parent_column(model) == parent_id, model.user_id == user_id)
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This client does not have access")

    await current.session.delete(membership)
    await current.session.flush()


async def client_emails_for_estimate(current: CurrentUser, estimate) -> list[str]:
    """Addresses to notify that an estimate is awaiting signature.

    The membership tables migration 0019 added are what make this
    answerable at all: before them there was no way to say WHICH of a
    company's clients an estimate belonged to, so "email the client" had no
    recipient to resolve — which is a large part of why send-for-signature
    notified nobody.

    Returns `[]` rather than raising when a document has no client attached
    yet. That is a legitimate state (an estimate built before the customer
    has an account), and refusing to send-for-signature over it would break
    the internal workflow to fix a notification.
    """
    emails: list[str] = []

    if estimate.project_id is not None:
        result = await current.session.execute(
            select(User.email)
            .join(ProjectClient, ProjectClient.user_id == User.id)
            .where(ProjectClient.project_id == estimate.project_id)
        )
        emails.extend(result.scalars().all())

    if estimate.lead_id is not None:
        result = await current.session.execute(
            select(User.email)
            .join(LeadClient, LeadClient.user_id == User.id)
            .where(LeadClient.lead_id == estimate.lead_id)
        )
        emails.extend(result.scalars().all())

    # An estimate can carry both a lead_id and a project_id (a won lead's
    # project), and the same person may be on both — dedupe so they get one
    # email, preserving order for a deterministic test.
    return list(dict.fromkeys(emails))


async def client_emails_for_project(current: CurrentUser, project_id: uuid.UUID) -> list[str]:
    result = await current.session.execute(
        select(User.email)
        .join(ProjectClient, ProjectClient.user_id == User.id)
        .where(ProjectClient.project_id == project_id)
    )
    return list(dict.fromkeys(result.scalars().all()))


async def company_display_name(current: CurrentUser, company_id: uuid.UUID) -> str:
    """The company's own name, for the "who is asking you to sign this"
    line in a client-facing email.

    Takes the DOCUMENT's company_id, never `current.company_id`: a
    parent-branch user may send a descendant branch's estimate, and the
    customer should see the branch they are actually doing business with —
    the same reasoning that governs which company_id gets stamped on the
    esignature.

    Falls back to the product name if the row is somehow unreadable, so a
    notification never goes out addressed from nobody.
    """
    result = await current.session.execute(select(Company.name).where(Company.id == company_id))
    return result.scalar_one_or_none() or "Builders Stream"
