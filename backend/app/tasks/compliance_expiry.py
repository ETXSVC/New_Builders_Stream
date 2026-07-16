"""Task 3.8: `check_compliance_expiry` scheduler actor.

This is the one place in this codebase that legitimately queries across all
tenants at once, and it is worth being explicit about why that is not a bug
or a shortcut.

`generate_estimate_pdf` (`app/tasks/estimate_pdf.py`, Task 2.15), the only
other actor defined so far, is scoped to ONE caller's ONE resource: it is
handed an `estimate_id` and a `requesting_user_id`, and resolves which
single tenant it belongs to (`_resolve_estimate_for_user`) before doing any
work. This actor has no such caller and no such resource. A daily
compliance-expiry scan has to look at EVERY company's compliance documents
in a single run — there is no "whose data is this" question to answer
first, because the whole point of the job is to answer it for all companies
at once. That is a structurally different shape of problem, not a variant
of the same one.

`app/db.py`'s `SessionLocal`/`engine` connect as the restricted `app_user`
role, which is RLS-constrained: without `app.current_tenant` set,
`get_all_descendant_ids(NULL)` (see migration `0001_initial_schema.py`)
returns zero rows, so a session with no tenant context set cannot see ANY
company's data — let alone every company's. Looping over companies and
calling `set_current_tenant` once per company was considered and rejected:
it still requires enumerating companies before the loop can start (the same
chicken-and-egg problem an `app_user` connection has for tenant-scoped
data), and it would mean N+1 round trips and N+1 transactions for what is
conceptually one scan.

The correct, and only legitimate, way to do a genuine cross-tenant scan
from application code is to connect using `settings.migrations_database_url`
(`app/config.py`), the same connection string every Alembic migration
already uses (`migrations/env.py`) and the same one this codebase's own
tenant-isolation regression tests connect with to legitimately bypass RLS
for test setup/teardown (`OWNER_DSN` in `tests/test_tenant_isolation_phase2.py`
and `tests/test_tenant_isolation_phase3.py`). That connection string
authenticates as the `postgres` table-owner role, which Postgres exempts
from RLS by default — there is no tenant context to set, and none is set
below, because the owner connection already sees everything unconditionally,
which is exactly what a company-wide scan needs.

This module therefore builds its OWN, separate, owner-role SQLAlchemy async
engine/sessionmaker, module-level in this file, rather than adding a second,
more-privileged engine to the shared `app/db.py` — that file's own module
comment states its `engine` "connects as the restricted `app_user` role",
and a second engine living there would blur that invariant for every other
piece of app code that imports from it. This owner-role engine is specific
to this one actor's genuinely cross-tenant need.

Session-factory / test-database resolution: `_check_compliance_expiry`
takes `session_factory` as a parameter (defaulting to this module's own
`_OwnerSessionLocal`) rather than hardcoding `_owner_engine` as the only
path, because tests run against `settings.test_database_url`, not the dev
database `settings.migrations_database_url` points at by default. This is
not a new problem: `tests/conftest.py` already solves the equivalent problem
for `settings.database_url` and `settings.migrations_database_url` alike, by
setting `DATABASE_URL`/`MIGRATIONS_DATABASE_URL` as OS environment variables
at conftest.py *module* import time — guaranteed (by pytest's own collection
order) to run before any test module's `from app.config import settings`
executes for the first time anywhere in the process. Because
`_owner_engine`/`_OwnerSessionLocal` below are themselves built from
`settings.migrations_database_url` at THIS module's import time, and this
module is only ever imported by test modules (which are always imported
after conftest.py), the module-level default already resolves correctly to
`builders_stream_test` under pytest and to the real dev database outside of
it — with no test-only branching logic anywhere in this file. The
`session_factory` parameter exists so tests can additionally pass an
explicit, unambiguous sessionmaker of their own construction (see
`tests/test_compliance_expiry_task.py`) rather than relying solely on that
env-var-timing guarantee, which keeps the test suite's proof of "this ran
against the test database" independent of this module's own default.
"""

from __future__ import annotations

from datetime import date

import dramatiq
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.core.tier_gating import tier_allows
from app.models.compliance_document import ComplianceDocument
from app.models.compliance_notification import VALID_THRESHOLDS, ComplianceNotification
from app.tasks import broker  # noqa: F401 - import-time side effect (see estimate_pdf.py's docstring)

# Owner-role engine: connects as the `postgres` table-owner role via
# `settings.migrations_database_url`, the same connection string Alembic
# migrations use (`migrations/env.py`). RLS does not apply to table owners,
# so no `set_current_tenant`/`set_current_user` call is needed or made
# anywhere in this module — see the module docstring for the full
# justification of why this is the one deliberate exception to "app code
# always goes through the RLS-constrained `app_user` connection."
_owner_engine = create_async_engine(settings.migrations_database_url, pool_pre_ping=True)
_OwnerSessionLocal = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)


async def _check_compliance_expiry(
    session_factory: async_sessionmaker[AsyncSession] = _OwnerSessionLocal,
) -> None:
    """Scans every company's `compliance_documents` rows (no tenant filter —
    see module docstring) and inserts one `compliance_notifications` row per
    `(compliance_document_id, threshold)` pair that has newly crossed its
    threshold and has not already fired.

    For each document, `days_until_expiry = (expires_on - today).days`; for
    each of `VALID_THRESHOLDS` (`30_day`, `14_day`, `7_day`), a notification
    is inserted if `days_until_expiry <= threshold_days` AND no existing
    `compliance_notifications` row already exists for that exact
    `(compliance_document_id, threshold)` pair. Thresholds are independent
    of each other and independent of run order: a document already 25 days
    from expiry on the very first run this logic ever executes against it
    fires both `30_day` and, since 25 <= 14 is false, only `30_day` in that
    run (a document at 10 days out on its first run would correctly fire
    both `30_day` and `14_day` in the same run). Each threshold fires at
    most once ever, enforced by the `UniqueConstraint` on
    `(compliance_document_id, threshold)` (Task 3.1) — checked here
    in-memory (pre-fetching every existing `(compliance_document_id,
    threshold)` pair once, rather than one query per document per
    threshold) purely to avoid unnecessary INSERT/constraint-violation
    round trips, not because the constraint itself is trusted alone to
    prevent duplicates; see the "second run does not duplicate" test for
    that exercised separately.
    """
    today = date.today()

    # Both queries below are unfiltered full-table scans, by design at the
    # current scale (code-quality review of this task flagged this
    # explicitly as a future scaling cliff, not a defect): every
    # ComplianceDocument and every historical ComplianceNotification ever
    # fired, across every tenant, loads into memory on each daily run. At
    # today's data volume this is far cheaper than N per-document
    # existence queries; once real notification-table volume is known, a
    # natural follow-up is windowing the `already_fired` prefetch (e.g. to
    # documents not yet expired, since an already-expired document's
    # thresholds have necessarily all fired already).
    async with session_factory() as session:
        documents_result = await session.execute(select(ComplianceDocument))
        documents = documents_result.scalars().all()

        already_fired_result = await session.execute(
            select(
                ComplianceNotification.compliance_document_id,
                ComplianceNotification.threshold,
            )
        )
        already_fired = {(doc_id, threshold) for doc_id, threshold in already_fired_result.all()}

        # Tier gating (Task 5.8 scope addition, spec Decision 4; hole found
        # by Task 5.4's code-quality review): this scan WRITES into the
        # Compliance module (pro+). Without this skip, a company downgraded
        # to starter keeps accumulating unread notifications it can never
        # dismiss (dismiss is a gated mutating route; reads stay open).
        # Checked once per distinct company_id, memoized across the document
        # loop — tier_allows is a plain SELECT on subscriptions via
        # get_root_company_id, which the owner-role session sees without any
        # tenant context (owner is RLS-exempt), so at most one extra query
        # per company per daily run, matching this actor's existing
        # "prefetch/loop in memory, no per-row round trips" shape.
        compliance_allowed: dict = {}

        for document in documents:
            allowed = compliance_allowed.get(document.company_id)
            if allowed is None:
                allowed = await tier_allows(session, document.company_id, "compliance")
                compliance_allowed[document.company_id] = allowed
            if not allowed:
                continue

            days_until_expiry = (document.expires_on - today).days

            for threshold in VALID_THRESHOLDS:
                threshold_days = int(threshold.split("_", 1)[0])
                if days_until_expiry > threshold_days:
                    continue
                if (document.id, threshold) in already_fired:
                    continue

                session.add(
                    ComplianceNotification(
                        company_id=document.company_id,
                        compliance_document_id=document.id,
                        threshold=threshold,
                    )
                )
                already_fired.add((document.id, threshold))

        await session.commit()


# The actual `@dramatiq.actor` — a thin wrapper around `_check_compliance_expiry`
# (see that function's own docstring, and estimate_pdf.py's `_generate_estimate_pdf`
# docstring, for why the undecorated-function/decorated-actor split exists:
# Dramatiq wraps every `async def` actor's `fn` in `async_to_sync()`, which
# requires a running worker's event loop thread; tests must be able to
# `await` the bare coroutine directly instead). `actor_name` keeps the queued
# message's actor name matching this module-level name rather than the
# wrapped function's own `__name__`. The scheduler service that calls
# `.send()` on this actor is Task 3.9's responsibility, not this task's —
# this task only needs the actor to exist and be directly callable.
check_compliance_expiry = dramatiq.actor(max_retries=3, actor_name="check_compliance_expiry")(
    _check_compliance_expiry
)
