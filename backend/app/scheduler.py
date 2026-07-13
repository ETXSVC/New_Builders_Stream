"""Task 3.9: the scheduler service that fires `check_compliance_expiry`
(Task 3.8, `app/tasks/compliance_expiry.py`) once a day.

This module is a standalone process entrypoint (`python -m app.scheduler`,
wired to the `scheduler` Docker Compose service), not something imported by
the FastAPI app or the `worker` service. It uses APScheduler's
`BlockingScheduler` — a scheduler that owns and blocks the process's own main
thread running its job loop, appropriate here because this process has no
other job: it does not serve HTTP requests and does not itself consume
Dramatiq messages, it only enqueues one.

Enqueue, not execute: the daily job below calls `check_compliance_expiry.send()`
(the `@dramatiq.actor`-wrapped object from `app/tasks/compliance_expiry.py`,
NOT the undecorated `_check_compliance_expiry` coroutine function) — `.send()`
publishes a message onto the Redis-backed broker (`app/tasks/broker.py`,
imported as an import-time side effect by `compliance_expiry.py` itself, so
no separate broker import is needed here) for a running `worker` process to
actually pick up and run. This scheduler process never runs
`_check_compliance_expiry` itself, and never opens a database connection of
its own — that separation is what lets the `scheduler` service stay a tiny,
single-purpose cron trigger while the actual (and heavier, DB-touching) scan
work stays on the `worker` service, consistent with how every other job in
this codebase is enqueued from wherever the triggering event happens
(`app/routers/estimates.py` for `generate_estimate_pdf`) and executed only by
`worker`.

`hour=2` is an arbitrary but reasonable off-peak default (Task 3.9's own
spec) — no other constraint in this codebase ties the compliance scan to a
specific hour.
"""

from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

from app.tasks.compliance_expiry import check_compliance_expiry


def _run_check_compliance_expiry() -> None:
    """The APScheduler job function itself. A thin sync wrapper around
    `check_compliance_expiry.send()` — `.send()` is a plain synchronous
    call (it just publishes a message to Redis), so no `async`/`await` or
    event-loop handling is needed here, unlike the actor's own body.
    """
    check_compliance_expiry.send()


if __name__ == "__main__":
    scheduler = BlockingScheduler()
    scheduler.add_job(_run_check_compliance_expiry, trigger="cron", hour=2)
    scheduler.start()
