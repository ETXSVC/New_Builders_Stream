"""`LEAD_WON` event handler (Task 1.18): drafts a Project the moment a Lead
transitions into `won`.

Registered against `app.core.events` by `app.core.event_handlers`'s
`register_event_handlers()`, which `app/main.py` calls once at real process
startup. That one-time, import-time registration is NOT enough for tests:
`app.core.events`' handler registry is process-lifetime module state, and
`tests/conftest.py`'s autouse `_clean_event_registry` fixture clears it
before *and* after every test (see that fixture's own docstring). A test
that needs this handler live must call `register_event_handlers()` (or
`app.core.events.register("LEAD_WON", handle_lead_won)` directly) itself —
same discipline `tests/test_lead_state_machine.py`'s
`test_transition_into_won_calls_publish_with_the_expected_payload` already
established for a hand-rolled capture handler, now applied to the real one.

Inherited Invariant #4 (reused throughout Phase 0/1, most recently by
`app/core/deps.get_current_user`'s docstring and `PATCH /projects/{id}`):
this handler MUST reuse the caller's `session` — the exact `AsyncSession`
the `PATCH /leads/{id}` route handler is using, passed through by
`app/routers/leads.py`'s `publish("LEAD_WON", session=current.session, ...)`
call — and must NEVER call `session.commit()` or `session.rollback()`
itself. `get_current_user` owns the single commit for the whole request
(design decision #8); this handler only ever `flush()`es, so that if it (or
anything else in the request) raises after this function runs,
`get_current_user`'s `except Exception: await session.rollback()` discards
the Lead status change AND this handler's Project/audit_log writes
together, not independently.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project
from app.services.audit import write_audit_log


async def handle_lead_won(
    *,
    session: AsyncSession,
    lead_id: uuid.UUID,
    company_id: uuid.UUID,
    contact_name: str,
    project_name: str,
    actor_id: uuid.UUID | None,
    **_ignored: object,
) -> None:
    """Drafts a `Project` from a won Lead and audits the draft.

    `company_id` MUST be the Lead's own `company_id`, not the acting
    caller's tenant context — `app/routers/leads.py`'s `publish()` call site
    passes `lead.company_id` specifically for this reason (found during this
    task's spec review: Task 1.17's hierarchical visibility means a parent
    company's admin can legally win a CHILD branch's Lead without header
    spoofing, and the two company_ids diverge in that case). This function
    doesn't re-derive or validate that — it trusts whatever `company_id` its
    caller supplies, same as every other keyword this handler accepts.

    Field mapping is Task 1.18's own spec, verbatim: `name=project_name`,
    `lead_id=lead_id`, `company_id=company_id`, `status="draft"`, and
    `site_address=""` (design decision #5 — `leads` carries no address, and
    `projects.site_address` is `NOT NULL`; the empty string is a deliberate
    placeholder a PM fills in later via `PATCH /projects/{id}`, not a bug).

    `contact_name` is part of the published `LEAD_WON` payload (Task 1.18's
    documented event shape) but isn't carried onto `Project` — the model has
    no contact-name column (`app/models/project.py`) — so it's accepted here
    only to match that payload shape; `**_ignored` absorbs any future payload
    keys this handler doesn't care about without breaking on `publish()`'s
    `**payload` forwarding.

    No `session.commit()`/`rollback()` here — see this module's docstring.
    """
    project = Project(
        company_id=company_id,
        lead_id=lead_id,
        name=project_name,
        site_address="",
        status="draft",
    )
    session.add(project)
    await session.flush()

    await write_audit_log(
        session,
        company_id=company_id,
        actor_id=actor_id,
        action="project.drafted_from_lead",
        entity_type="project",
        entity_id=project.id,
        metadata={"lead_id": str(lead_id)},
    )
