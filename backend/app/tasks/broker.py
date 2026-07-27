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

import logging

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import Middleware
from dramatiq.middleware.asyncio import AsyncIO

from app.config import settings
from app.core.observability import init_error_reporting

logger = logging.getLogger(__name__)


class DeadLetterLogging(Middleware):
    """Log every message Dramatiq gives up on.

    Retry exhaustion was completely silent. `after_nack` fires when a
    message is nacked into the dead-letter queue — i.e. after `max_retries`
    is spent — and nothing was listening, so an actor that failed three
    times produced no record an operator could find. The review doc filed
    this against `send_invitation_email` as "the one actor with no
    bookkeeping row", which is true but is the wrong level to fix it at:

      * that actor's docstring **deliberately** rejects a sent/failed
        table, and the reasoning is sound — the invitation row and its
        copyable accept link are the source of truth, and the email is an
        optimization on top of them, not the only way in. Adding a table
        would contradict a decision someone made on purpose;
      * and it is not actually the only actor without one. `estimate_pdf`,
        `compliance_expiry`, `seat_usage` and both other mailers have no
        per-message bookkeeping either. Only `accounting_sync` does, and
        that is because `integration_sync_records` is a product surface
        (`GET /integrations/{provider}/sync-status`), not an ops one.

    So this is one middleware covering every actor, present and future,
    instead of N tables. It gives the operator the half that was missing —
    "this job died" — while `app/core/after_commit.py`'s own logging
    covers the other half, "this job was never enqueued".

    Deliberately logging only. It does not retry, alert, or write a row:
    the log line is the artifact, `docker logs` + grep is the stated
    consumer (see `app/core/logging.py`), and anything richer wants the
    Sentry integration `docs/11-production-deployment.md` already tracks as
    a deferred follow-up.
    """

    def after_nack(self, broker, message):
        logger.error(
            "dramatiq message dead-lettered after exhausting retries: "
            "actor=%s message_id=%s args=%s kwargs=%s",
            message.actor_name,
            message.message_id,
            message.args,
            message.kwargs,
        )


# The worker has no FastAPI startup of its own — the dramatiq CLI imports
# the actor modules named on its command line, and every one of them
# imports this module for the broker side effect. So this is the only
# place that reliably runs once per worker process.
init_error_reporting("worker")

redis_broker = RedisBroker(url=settings.redis_url)
redis_broker.add_middleware(AsyncIO())
redis_broker.add_middleware(DeadLetterLogging())
dramatiq.set_broker(redis_broker)
