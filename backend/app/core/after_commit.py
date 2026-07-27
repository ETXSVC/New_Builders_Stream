"""Defer a side effect until the request transaction has actually committed.

The problem this exists for: `app/services/financial_record_sync_handler.py`
called `sync_financial_record.send(...)` from inside an event handler, and
event handlers run **inside** the request transaction (`app/core/events.py`
dispatches inline, deliberately, so a handler's exception can roll the whole
thing back). Redis does not participate in that transaction. So the message
was already queued while the row it referred to might still be rolled back.

The failure was not hypothetical. A worker picking that message up finds no
such invoice/bill/expense, raises, and Dramatiq retries — three times, with
backoff, against a row that will never exist. The queue absorbs the cost
quietly, which is why nobody noticed.

Ordering it the other way round is the fix: commit first, enqueue after.
`get_current_user` (`app/core/deps.py`) is the one place that owns the
commit, so it is the one place that drains this.

**What this does not do.** If the process dies in the window between
`COMMIT` returning and the enqueue running, the message is lost — the row
exists and nothing will ever sync it. That window is microseconds and
requires a crash inside it, versus the previous bug which fired on every
ordinary rollback. Closing it completely needs a transactional outbox: a
table written in the same transaction and drained by a poller, so the
enqueue is as durable as the row. That is the right shape if sync
reliability ever becomes a stated requirement; it is a table, an RLS
policy, a migration and a drain job, which is not proportionate to the
failure being fixed today. Recorded here rather than in a backlog nobody
reads.

A callback that raises is **logged and swallowed**, not propagated. By the
time these run the transaction is durable: the invoice exists, and the
caller's 201 is the truth. Turning a queue hiccup into a 500 would tell
them their invoice was not created, which is false. The log line is the
failure surface — paired with the dead-letter logging in
`app/tasks/broker.py`, an operator can see both halves (never enqueued /
enqueued but never succeeded).
"""
import logging
from typing import Any, Callable

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Key into `Session.info`, a plain dict SQLAlchemy hands us for exactly this
# kind of per-session bookkeeping. Namespaced so it cannot collide with
# anything SQLAlchemy or another library keeps there.
_PENDING_KEY = "builders_stream.after_commit"


def enqueue_after_commit(session: AsyncSession, send: Callable[..., Any], /, **kwargs: Any) -> None:
    """Queue `send(**kwargs)` to run once `session` commits.

    Takes the bound `.send` of a Dramatiq actor rather than the actor
    itself, so a caller cannot accidentally pass the undecorated function
    and have this run the job inline.
    """
    pending: list[Callable[[], Any]] = session.info.setdefault(_PENDING_KEY, [])
    pending.append(lambda: send(**kwargs))


def run_after_commit(session: AsyncSession) -> None:
    """Run and clear everything queued on `session`. Called by
    `get_current_user` immediately after `await session.commit()`.

    Pops before iterating so a callback that itself queues more work cannot
    loop, and so a raised-and-logged callback is not retried on some later
    commit of the same session.
    """
    pending: list[Callable[[], Any]] = session.info.pop(_PENDING_KEY, [])
    for callback in pending:
        try:
            callback()
        except Exception:
            # See the module docstring: the data is already durable, so
            # this must not become the caller's problem. Loud in the log,
            # invisible in the response.
            logger.exception("after-commit callback failed; the enqueue was lost")


def pending_count(session: AsyncSession) -> int:
    """How many callbacks are queued. For tests — asserting on a count is
    what distinguishes "deferred correctly" from "never registered at all",
    and the latter would pass any test that only checks nothing was sent
    during the transaction.
    """
    return len(session.info.get(_PENDING_KEY, []))
