"""Generic cursor-based pagination helpers for list endpoints.

`GET /leads` (Task 1.4) is the first paginated list endpoint in this
codebase; Projects/Tasks/Documents (later Phase 1 tasks) are expected to
reuse this exact module rather than reinvent pagination per-router.

Design: offset pagination (`?page=`/`?offset=`) is the tempting default but
has two real problems on a growing, concurrently-written table: (1) `OFFSET`
forces Postgres to scan and discard N rows before returning anything, which
gets linearly more expensive as a company accumulates leads; (2) it is
unstable under concurrent inserts — a row inserted ahead of the cursor
position between two page fetches shifts every subsequent offset by one,
silently skipping or duplicating rows for a caller mid-walk. Cursor
pagination avoids both: it remembers *where the last page ended* (a specific
row's sort key) and asks for "strictly after that," which stays correct
regardless of what else gets inserted elsewhere in the table.

The sort/cursor key is the composite `(created_at, id)`, not `created_at`
alone. This mirrors the exact lesson Phase 0 already paid for: relying on
`created_at`-only ordering with no tiebreaker (`company_users` lookups via
`.first()`) produced unstable results whenever two rows shared a timestamp
(bulk inserts, or two requests landing in the same tick — `created_at` has
finite, not infinite, resolution). `id` (UUID) carries no ordering meaning
of its own, but it is unique and immutable, which is all a tiebreaker needs
to be: it guarantees a strict total order over the table.

The cursor itself is an opaque, base64-encoded token, not a raw offset or a
client-constructable `created_at`/`id` pair. This isn't a security boundary
(nothing sensitive is encoded — it's an opaque re-statement of public sort
positions), it's an API-stability one: callers are expected to pass the
`next_cursor` value back verbatim and never construct or parse it
themselves, so the internal encoding is free to change later without that
being a breaking API change.
"""

import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import date, datetime
from typing import TypeVar

from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

_CURSOR_SEPARATOR = "|"

_Row = TypeVar("_Row")


class InvalidCursorError(ValueError):
    """Raised when a client-supplied cursor can't be decoded. `paginate()`
    catches this itself (see below) — a route handler using `paginate()`
    never needs to catch this directly."""


def encode_cursor(created_at: datetime, id_: uuid.UUID) -> str:
    raw = f"{created_at.isoformat()}{_CURSOR_SEPARATOR}{id_}"
    return urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        created_at_str, id_str = raw.rsplit(_CURSOR_SEPARATOR, 1)
        created_at = datetime.fromisoformat(created_at_str)
        id_ = uuid.UUID(id_str)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any malformed
        # input (bad base64, wrong separator count, unparsable timestamp,
        # invalid UUID) is equally "not a valid cursor" from the caller's
        # perspective and should collapse to the same InvalidCursorError.
        raise InvalidCursorError("Invalid pagination cursor") from exc
    return created_at, id_


async def paginate(
    session: AsyncSession,
    query: Select[tuple[_Row]],
    *,
    created_at_col: InstrumentedAttribute[datetime] | InstrumentedAttribute[date],
    id_col: InstrumentedAttribute[uuid.UUID],
    cursor: str | None,
    limit: int,
) -> tuple[list[_Row], str | None]:
    """Applies cursor filtering, tie-broken ordering, and limit+1 fetch/trim
    to `query`, returning `(rows, next_cursor)`. This is the actual
    reusable half of cursor pagination (see module docstring) — every list
    endpoint should call this rather than re-deriving the composite
    tuple_(created_at, id) comparison and fetch-and-trim logic by hand.
    `query` should already have any non-pagination filters (status, etc.)
    applied; do not call `.order_by()`/`.limit()` on it yourself — this
    function owns both.

    Raises InvalidCursorError for a malformed cursor — callers don't need
    their own try/except; FastAPI's exception handler (app/main.py)
    translates it into a 400 automatically.

    A cursor pointing at a since-deleted (created_at, id) pair resumes
    cleanly from the next surviving row: the WHERE clause is a strict
    inequality against a sort position, not a lookup of a specific row, so
    the referenced row's continued existence is never required.
    """
    if cursor is not None:
        cursor_created_at, cursor_id = decode_cursor(cursor)
        query = query.where(tuple_(created_at_col, id_col) > (cursor_created_at, cursor_id))

    # Fetch one extra row (limit + 1) to learn whether a next page exists
    # without a second COUNT/EXISTS query; the extra row is trimmed below
    # and never returned to the caller.
    query = query.order_by(created_at_col.asc(), id_col.asc()).limit(limit + 1)

    result = await session.execute(query)
    rows = list(result.scalars().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        # .key is the mapped attribute's name (e.g. "created_at") — this is
        # how we read the value back off a returned row generically, without
        # the caller needing to also pass "how to get created_at off a row".
        next_cursor = encode_cursor(getattr(last, created_at_col.key), getattr(last, id_col.key))

    return rows, next_cursor
