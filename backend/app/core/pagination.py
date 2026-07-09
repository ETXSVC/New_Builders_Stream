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
from datetime import datetime

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

_CURSOR_SEPARATOR = "|"


class InvalidCursorError(ValueError):
    """Raised when a client-supplied cursor can't be decoded. Route handlers
    should catch this and translate it into a 400, not let it surface as an
    unhandled 500."""


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
