"""Minimal in-process, synchronous event dispatcher (design decision #2,
Phase 1 plan: "The `LEAD_WON` event bus is in-process and synchronous for
Phase 1, not Redis-backed").

Scope note (why this file exists ahead of Task 1.6): Task 1.6 ("In-Process
Event Bus") is the task formally scoped to build this module. Task 1.5
("Lead Status State Machine") needs a real, callable `publish()` — not a
comment/TODO — the moment a Lead transitions into `won`, per that task's own
text: "keep the `publish()` call itself in this task... so [the later task
wiring a consumer] is purely 'add a handler,' not 'add the publish call
too.'" Since Task 1.6 hasn't run yet, Task 1.5 builds the minimal dispatcher
surface Task 1.6's own spec describes, so `publish()` is real and inert (zero
registered handlers = no-op) rather than faked. Task 1.6 should treat this
file as already satisfying its module-creation step and focus on
`test_events.py` coverage for the dispatcher mechanics themselves (or extend
this docstring/module if its own review finds a gap) — see the Task 1.5
implementation report for the full scope split.

Design surface, matching Task 1.6's spec:
- `register(event_name, handler)`: subscribe a handler to an event name.
- `publish(event_name, **payload)`: call every handler registered for that
  event name, in registration order, propagating the first handler's
  exception rather than swallowing it (a handler failing mid-request must be
  able to roll back the whole transaction, matching the ACID expectations
  Phase 0 established for `get_current_user`, design decision #8).
- An event name with zero registered handlers is a no-op — nothing is
  required to subscribe.

One deliberate refinement beyond Task 1.6's literal text: `publish()` is
`async def` and `await`s each handler in sequence (not `asyncio.gather`,
so ordering and exception propagation stay strictly sequential — "the first
handler's exception propagates" only has one interpretation if handlers run
one at a time). This is necessary, not optional, in this codebase: the only
real consumer (the `LEAD_WON` handler that will draft a Project) needs to
perform async ORM work against `current.session` (an `AsyncSession`), and a
synchronous dispatcher couldn't `await` that. "Synchronous" in the design
decision's wording means "dispatched inline, in the same request/transaction,
not queued" — not "non-async Python callables." Handlers registered here are
expected to be `async def` callables.
"""

from collections import defaultdict
from typing import Awaitable, Callable

EventHandler = Callable[..., Awaitable[None]]

_handlers: dict[str, list[EventHandler]] = defaultdict(list)


def register(event_name: str, handler: EventHandler) -> None:
    """Subscribe `handler` to `event_name`. Multiple handlers for the same
    event are all called, in registration order, when that event publishes."""
    _handlers[event_name].append(handler)


def is_registered(event_name: str, handler: EventHandler) -> bool:
    """True if `handler` is already subscribed to `event_name`. Added in
    Task 1.18 for callers whose registration call may legitimately run more
    than once per process — namely `app.core.event_handlers.
    register_event_handlers()`, which real app startup (`app/main.py`, at
    module-import time) and tests (explicitly, per test, since
    `tests/conftest.py`'s autouse `_clean_event_registry` fixture clears
    this module's registry before/after every test) can both end up calling
    for the same handler within one process: the first test that triggers
    `app.main`'s own module-level import (via the `client` fixture) races
    that import's registration against the test's own explicit one. Guarding
    with this before calling `register()` keeps registration idempotent
    without changing `register()`'s own documented "always append" contract
    (still exercised as-is by every test in `test_events.py`)."""
    return handler in _handlers.get(event_name, [])


async def publish(event_name: str, **payload: object) -> None:
    """Call every handler registered for `event_name`, in registration
    order, passing `payload` as keyword arguments. A no-op if nothing is
    registered for `event_name`. Propagates the first handler's exception —
    does not swallow it and does not run subsequent handlers after one
    raises."""
    for handler in _handlers.get(event_name, []):
        await handler(**payload)


def clear() -> None:
    """Deregisters every handler for every event. `_handlers` is
    process-lifetime module state, not reset between requests the way DB
    rows are reset between tests by conftest.py's `_clean_tables` — tests
    that register a handler must not rely on remembering to reach into the
    private `_handlers` dict themselves; call this (ideally via an autouse
    fixture) instead, so a test that fails before its own manual cleanup
    can't leak a handler into later tests."""
    _handlers.clear()
