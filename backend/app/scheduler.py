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

This module deliberately has NO `MIGRATIONS_DATABASE_URL` override in
`docker-compose.yml` (unlike `worker`) — it must never call
`_check_compliance_expiry` directly, only `.send()` the decorated actor.
If a future change makes this process touch the database directly, it will
fail with the exact `OSError: Connect call failed ('127.0.0.1', 5432)`
Task 3.9's own commit fixed for `worker`, since `scheduler`'s container
still resolves `MIGRATIONS_DATABASE_URL` to its host-side-only `localhost`
value — that failure would be the signal this module took on a
responsibility it was never meant to have.
"""

from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

from app.tasks.compliance_expiry import check_compliance_expiry
from app.tasks.seat_usage import report_seat_usage

# A brief outage spanning 2am (container restart, host reboot) would
# otherwise cause APScheduler's own 1-second default misfire_grace_time to
# silently skip that day's compliance scan entirely, with no error and no
# visible signal anywhere — a bigger consequence for a compliance-deadline
# feature than for a typical cron job. A few hours of grace lets a missed
# run still fire late rather than vanishing until the next scheduled day.
_MISFIRE_GRACE_TIME_SECONDS = 4 * 60 * 60


def _run_check_compliance_expiry() -> None:
    """The APScheduler job function itself — not just `check_compliance_expiry.send`
    passed directly to `add_job` (which would also work; `.send` is a plain
    bound method APScheduler could call as-is). Kept as a named wrapper for
    two reasons: (1) APScheduler derives a job's log/repr identity from the
    callable's own `__name__`, and a `dramatiq.Actor` bound method reprs
    poorly there, while `_run_check_compliance_expiry` is immediately
    legible in scheduler logs; (2) it is the one seam in this file that is
    actually unit-testable in isolation (mock `check_compliance_expiry.send`,
    call this function, assert called-once) — passing `.send` straight into
    `add_job` would remove that seam entirely, leaving nothing short of a
    full `BlockingScheduler` integration test able to verify the wiring.
    """
    check_compliance_expiry.send()


def _run_report_seat_usage() -> None:
    """Same wrapper rationale as _run_check_compliance_expiry above: a
    named, log-legible seam that's independently unit-testable (mock
    report_seat_usage.send, call this, assert called-once)."""
    report_seat_usage.send()


if __name__ == "__main__":
    scheduler = BlockingScheduler()
    scheduler.add_job(
        _run_check_compliance_expiry,
        trigger="cron",
        hour=2,
        misfire_grace_time=_MISFIRE_GRACE_TIME_SECONDS,
    )
    scheduler.add_job(
        _run_report_seat_usage,
        trigger="cron",
        hour=3,
        misfire_grace_time=_MISFIRE_GRACE_TIME_SECONDS,
    )
    scheduler.start()
