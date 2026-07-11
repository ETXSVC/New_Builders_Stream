"""Task 2.14: Dramatiq broker configuration — the first real async task
queue in this codebase (Phase 1's own design decision #2 deliberately
deferred this; `LEAD_WON`'s single in-process, same-transaction consumer
didn't need one). PDF export (Task 2.15) is the first job that genuinely
does, per [Technical Architecture](../../../docs/03-technical-architecture.md)
Section 7 naming "Celery or Dramatiq + Redis" for PDF generation.

Configures a `RedisBroker` using `settings.redis_url` (added to
`app/config.py` in Phase 0, unused until now) and registers it as
Dramatiq's global default broker at IMPORT TIME (`dramatiq.set_broker(...)`
runs as a module-level side effect, not inside a function) — Dramatiq's
`@dramatiq.actor` decorator and `.send()` enqueue call both resolve the
broker via `dramatiq.get_broker()` at the time they're used, so this module
must be imported, and therefore this side effect must have already run,
before any actor is defined or any message is enqueued anywhere in the
process. Two entry points will need to import this module first once
Task 2.15 lands: whichever request-handling module ends up calling
`.send()` to enqueue a job (as of this task, nothing in the request path
does yet — that wiring is Task 2.15's), and the worker's own CLI
entrypoint (`dramatiq app.tasks.estimate_pdf`, also Task 2.15) —
`dramatiq`'s CLI imports the named module before running its event loop,
and that module will itself import this one before defining its actor(s),
for the same reason.

No actor is defined in this module, or anywhere yet, per this task's own
scope — Task 2.15 adds the first one (`app/tasks/estimate_pdf.py`).

`AsyncIO` middleware, added during Task 2.15: this codebase's actors are
`async def` (Task 2.15's `generate_estimate_pdf` needs `await`-based DB
access via `app/db.py`'s `SessionLocal`), but Dramatiq's `default_middleware`
(`dramatiq/middleware/__init__.py`) does NOT include `AsyncIO` — it must be
added explicitly, or an async actor's coroutine is returned but never
actually driven to completion (empirically confirmed: without this
middleware, a message sent to an `async def` actor never finishes — no
exception, the coroutine object is simply never awaited). `AsyncIO`
manages a dedicated background event-loop thread the worker process uses
to run every async actor's coroutine to completion.
"""

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware.asyncio import AsyncIO

from app.config import settings

redis_broker = RedisBroker(url=settings.redis_url)
redis_broker.add_middleware(AsyncIO())
dramatiq.set_broker(redis_broker)
