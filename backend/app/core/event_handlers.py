"""Wires real handlers into `app.core.events` (Task 1.18).

A dedicated module rather than inlining `register()` calls in
`app/main.py` or `app/services/lead_transitions.py`, for two reasons:

1. `app/services/lead_transitions.py` is a pure transition-table module
   (`LEAD_TRANSITIONS` + `is_legal_transition()`) with no side effects and no
   existing import of `app.core.events` — bolting a module-level
   `register()` call onto it would also be actively wrong, not just
   inconsistent: `tests/conftest.py`'s autouse `_clean_event_registry`
   fixture clears the process-global handler registry before *and* after
   every test, but Python only executes a module's top-level code once per
   process (import caching). A `register()` call sitting at
   `lead_transitions.py`'s module level would fire exactly once — the first
   time anything imports that module — and then get wiped by the very first
   test's `_clean_event_registry` "before" clear, never to return, silently
   breaking every subsequent test that expects `LEAD_WON` to draft a
   Project. The same trap applies to registering directly at `app/main.py`
   module level: `app.main` is imported once per test session (cached), so
   only the first test to trigger that import would ever see the handler
   registered.
2. Centralizing every `register()` call here — instead of scattering them
   across whichever router or model module happens to "own" an event — gives
   both real app startup (`app/main.py`, which calls
   `register_event_handlers()` once per process) and tests (which call it
   explicitly, per test, after `_clean_event_registry` has cleared the
   registry) one obvious, single place to import from.
"""

from app.core.events import is_registered, register
from app.services.estimate_approved_handler import handle_estimate_approved
from app.services.financial_record_sync_handler import handle_financial_record_created
from app.services.lead_won_handler import handle_lead_won
from app.services.project_completed_handler import handle_project_completed


def register_event_handlers() -> None:
    """Subscribes every real (non-test) event handler this app ships.

    Guarded with `is_registered()` because this function is, in practice,
    called more than once per process without an intervening
    `events.clear()` in between: `app/main.py` calls it at module-import
    time, and `app.main` is imported (via `tests/conftest.py`'s `client`
    fixture) exactly once per test *session*, the first time any test
    requests that fixture. Every test in `tests/test_lead_won_drafts_project.py`
    also calls this function explicitly, right after the autouse
    `_clean_event_registry` fixture's "before" clear. For whichever test
    happens to be first to trigger the `app.main` import, both of those
    calls land inside the same (post-clear, empty) registry — without this
    guard, `handle_lead_won` would end up registered twice for that one
    test, silently drafting two Projects per won Lead instead of one. This
    was caught empirically (an early version of the test file failed with
    "2 == 1" projects) before this guard was added, not reasoned out in
    advance."""
    if not is_registered("LEAD_WON", handle_lead_won):
        register("LEAD_WON", handle_lead_won)

    if not is_registered("ESTIMATE_APPROVED", handle_estimate_approved):
        register("ESTIMATE_APPROVED", handle_estimate_approved)

    if not is_registered("PROJECT_COMPLETED", handle_project_completed):
        register("PROJECT_COMPLETED", handle_project_completed)

    if not is_registered("INVOICE_CREATED", handle_financial_record_created):
        register("INVOICE_CREATED", handle_financial_record_created)

    if not is_registered("EXPENSE_CREATED", handle_financial_record_created):
        register("EXPENSE_CREATED", handle_financial_record_created)

    if not is_registered("BILL_CREATED", handle_financial_record_created):
        register("BILL_CREATED", handle_financial_record_created)
