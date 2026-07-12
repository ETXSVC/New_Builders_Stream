"""Task 2.14 (stub) + Task 2.15 (real actor, this extension): PDF export —
async job wiring.

`generate_estimate_pdf` is the first `@dramatiq.actor` this codebase
defines. It is `async def` (needs `await`-based DB access via
`app/db.py`'s `SessionLocal`), which is why `app/tasks/broker.py` had to add
Dramatiq's `AsyncIO` middleware before this could work at all (see that
module's docstring) — this file's own `from app.tasks import broker` import
below runs that middleware registration as an import-time side effect,
before `@dramatiq.actor` is ever evaluated.

Signature is `(estimate_id: str, requesting_user_id: str)`, not
`(estimate_id: str)` alone (resolved judgment call #1): Dramatiq messages
are JSON-serialized, so both are plain strings, parsed back into `uuid.UUID`
inside the actor. `requesting_user_id` — the admin/PM who called `POST
/estimates/{id}/export` — is captured at ENQUEUE time
(`app/routers/estimates.py`) and passed through the message payload
specifically so this actor can call `set_current_user(session, user_id)`
(`app/db.py`) with a real, non-optional user id: that function has no
"actor_id=None" option, and fabricating a placeholder id would be actively
wrong. This is purely for RLS-context consistency (Inherited Invariant #3's
documented exception letting worker code manage its own session/tenant
context explicitly) — it is NOT used to write an audit_log entry. PDF
export/generation is not on `docs/07-security-compliance.md` Section 5's
enumerated audit-worthy action list, matching this codebase's established
"not every action needs an audit_log entry" precedent (Cost Catalog/Markup
Profile/Communication Log creation).

Tenant-context chicken-and-egg problem: `estimates`' only RLS policy is the
ordinary `tenant_isolation` (`FOR ALL`, requires `app.current_tenant` to
already be set to the estimate's own company) — there is no `self_membership`-
style fallback for `estimates` the way `company_users` has one for itself.
But the actor's only inputs are `estimate_id` and `requesting_user_id`; it
has no `company_id` to set as tenant context before it has even looked up
the Estimate. This is resolved via `_resolve_estimate_for_user` below:
`company_users` DOES have a `self_membership` policy (`user_id =
app.current_user_id`, no tenant context required at all — the same policy
`get_current_user`, `app/core/deps.py`, already relies on to resolve a
caller's own memberships before `set_current_tenant` is called for the
first time in the ordinary request path). This actor queries that
self-visible set of the requesting user's own `(company_id)` memberships,
then tries `set_current_tenant` with each candidate in turn, re-querying for
the target Estimate after each attempt, until one succeeds or the
candidates are exhausted. In the overwhelmingly common case (a user who
belongs to exactly one company) this is a single attempt; it also handles a
user with legitimate membership in multiple companies (e.g. via
cross-company invitations) correctly, without requiring a third parameter
on the actor's own signature.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import dramatiq
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, set_current_tenant, set_current_user
from app.models import Company, CompanyUser, CostCatalogItem, Estimate, EstimateLineItem, MarkupProfile
from app.services.document_storage import write_estimate_pdf_file
from app.services.pdf_export import EstimateLineItemDisplay, render_estimate_pdf
from app.tasks import broker  # noqa: F401 - import-time side effect (see module docstring)


async def _resolve_estimate_for_user(
    session: AsyncSession, estimate_id: uuid.UUID, requesting_user_id: str
) -> Estimate | None:
    """Finds `estimate_id`, setting `app.current_tenant` (via
    `set_current_tenant`) to whichever of the requesting user's own
    companies actually owns it. Assumes `set_current_user` has already been
    called on `session` for this transaction (required for the
    `self_membership` policy the `company_users` lookup below relies on).
    Returns `None`, having left `app.current_tenant` at its last-tried
    value, if no candidate company owns this estimate (not found, or a
    stale/bogus `estimate_id`) — the caller is responsible for treating that
    as an error.
    """
    membership_result = await session.execute(
        select(CompanyUser.company_id).where(
            CompanyUser.user_id == uuid.UUID(requesting_user_id)
        )
    )
    candidate_company_ids = [row[0] for row in membership_result.all()]

    for company_id in candidate_company_ids:
        await set_current_tenant(session, str(company_id))
        result = await session.execute(select(Estimate).where(Estimate.id == estimate_id))
        estimate = result.scalar_one_or_none()
        if estimate is not None:
            return estimate

    return None


async def _generate_and_persist(estimate_id: uuid.UUID, requesting_user_id: str) -> None:
    """The actor's real work, in its own request-scoped-style transaction
    (opened and committed/rolled-back entirely within this function) — kept
    separate from `generate_estimate_pdf` itself so the failure path
    (`_mark_pdf_failed`) can cleanly open its OWN fresh session/transaction
    after this one has already been rolled back and closed, per this task's
    own "the failure handling itself must not be lost if the original
    transaction is what's rolling back" requirement.
    """
    session = SessionLocal()
    try:
        await session.begin()
        await set_current_user(session, requesting_user_id)

        estimate = await _resolve_estimate_for_user(session, estimate_id, requesting_user_id)
        if estimate is None:
            raise LookupError(
                f"Estimate {estimate_id} not found or not visible to user {requesting_user_id}"
            )

        # Same join shape as `app/services/estimate_calculation.py`'s
        # `calculate_estimate` (`EstimateLineItem` joined to
        # `CostCatalogItem` for `category`), extended to also select `name`
        # — the exact pair `EstimateLineItemDisplay` (Task 2.13) needs and
        # cannot look up itself (that module has no DB access).
        line_items_result = await session.execute(
            select(EstimateLineItem, CostCatalogItem.category, CostCatalogItem.name)
            .join(CostCatalogItem, EstimateLineItem.cost_catalog_item_id == CostCatalogItem.id)
            .where(EstimateLineItem.estimate_id == estimate.id)
            .order_by(EstimateLineItem.id.asc())
        )
        line_items = [
            EstimateLineItemDisplay(line_item=line_item, category=category, name=name)
            for line_item, category, name in line_items_result.all()
        ]

        # NOT NULL FK, same `scalar_one()` (not `scalar_one_or_none()`)
        # reasoning as `calculate_estimate`'s identical lookup.
        markup_result = await session.execute(
            select(MarkupProfile).where(MarkupProfile.id == estimate.markup_profile_id)
        )
        markup_profile = markup_result.scalar_one()

        # `estimate.company_id` is already confirmed as the tenant
        # `_resolve_estimate_for_user` just set app.current_tenant to,
        # so this SELECT is visible under the companies `tenant_select`
        # policy (which includes the tenant's own id — see
        # get_all_descendant_ids' base case).
        company_result = await session.execute(select(Company).where(Company.id == estimate.company_id))
        company = company_result.scalar_one()

        pdf_bytes = render_estimate_pdf(estimate, line_items, markup_profile, company.name)

        relative_path = write_estimate_pdf_file(
            company_id=estimate.company_id, estimate_id=estimate.id, content=pdf_bytes
        )

        estimate.pdf_status = "ready"
        estimate.pdf_storage_path = relative_path
        estimate.pdf_generated_at = datetime.now(timezone.utc)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _mark_pdf_failed(estimate_id: uuid.UUID, requesting_user_id: str) -> None:
    """Runs in a SEPARATE, fresh `SessionLocal()` transaction from whatever
    just failed in `_generate_and_persist` — that transaction has already
    been rolled back (and would roll back any `pdf_status` write attempted
    inside it) by the time this runs, so marking the failure requires its
    own independent session/transaction, not a reuse of the failed one.
    """
    session = SessionLocal()
    try:
        await session.begin()
        await set_current_user(session, requesting_user_id)

        estimate = await _resolve_estimate_for_user(session, estimate_id, requesting_user_id)
        if estimate is not None:
            estimate.pdf_status = "failed"
            await session.commit()
        else:
            # Nothing to mark — the estimate itself couldn't be resolved
            # for this user (e.g. it was deleted, or the failure happened
            # before/during resolution itself).
            await session.rollback()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def _generate_estimate_pdf(estimate_id: str, requesting_user_id: str) -> None:
    """Renders `estimate_id`'s current line items/markup/company to PDF
    (`app/services/pdf_export.py`, Task 2.13) and persists the result to
    disk (`write_estimate_pdf_file`) plus `estimate.pdf_status='ready'` /
    `pdf_storage_path` / `pdf_generated_at`.

    On any exception during rendering/writing, `pdf_status` is set to
    `'failed'` in a separate, fresh transaction (`_mark_pdf_failed`) and the
    original exception is re-raised, so Dramatiq's own retry/dead-letter
    handling (`max_retries=3` on the `generate_estimate_pdf` actor below,
    Dramatiq's default backoff) still applies on top of this codebase's own
    status tracking — the two are independent: a transient failure can retry
    and eventually reach `'ready'`, while `'failed'` is visible to `GET
    /estimates/{id}` pollers the whole time in between, rather than the
    field staying stuck on `'pending'` forever.

    See the module docstring for why the signature takes
    `requesting_user_id` (not just `estimate_id`) and how tenant context is
    resolved from it.

    Deliberately a plain, undecorated `async def` — NOT itself the
    `@dramatiq.actor` (see `generate_estimate_pdf` below, which wraps this
    one) — so tests can `await` it directly as an ordinary coroutine
    function, per this task's own "call it as a plain async function ...
    NOT through the full Dramatiq broker/worker round-trip" test-design
    instruction. This split is required, not merely convenient: Dramatiq
    wraps every `async def` actor's `fn` in `async_to_sync()`
    (`dramatiq/asyncio.py`), which routes every call through a background
    `EventLoopThread` that only exists once a real `dramatiq.Worker` has
    booted (`AsyncIO.before_worker_boot`) — calling the decorated `Actor`
    object directly outside of a running worker raises `RuntimeError:
    Global event loop thread not set`, empirically confirmed while writing
    this task's tests. Keeping the real implementation as a bare coroutine
    function sidesteps that entirely: it can be awaited directly, exactly
    like any other async function, and `generate_estimate_pdf.send(...)`
    (`app/routers/estimates.py`) still goes through the normal Dramatiq
    broker/worker path unaffected.
    """
    estimate_uuid = uuid.UUID(estimate_id)
    try:
        await _generate_and_persist(estimate_uuid, requesting_user_id)
    except Exception:
        await _mark_pdf_failed(estimate_uuid, requesting_user_id)
        raise


# The actual `@dramatiq.actor` — a thin wrapper around `_generate_estimate_pdf`
# (see that function's own docstring for why the split exists). This is the
# object `app/routers/estimates.py` calls `.send()` on to enqueue a job;
# `actor_name="generate_estimate_pdf"` keeps the queued message's actor name
# matching this module-level name (Dramatiq would otherwise use
# `_generate_estimate_pdf`'s name, the wrapped function's `__name__`).
generate_estimate_pdf = dramatiq.actor(max_retries=3, actor_name="generate_estimate_pdf")(
    _generate_estimate_pdf
)
