"""Optimistic concurrency control for the multi-editor records.

The problem this closes: two project managers open the same Project, both
edit, both save. The second write silently overwrites the first, and neither
person is told anything. There is no `version_id_col`, no ETag, and no live
update channel in this app, so before this module the only outcome available
was last-write-wins.

**Why a request-body field and not `If-Match`/ETag.** The HTTP-native answer
is a strong ETag on the GET plus `If-Match` on the PATCH, and that would be
the right shape in a vacuum. It is the wrong shape *here*: every browser
request reaches this API through the Next.js BFF, whose `apiFetch`
(`frontend/lib/api/client.ts`) constructs a fixed header allowlist —
`Content-Type`, `Authorization`, `X-Tenant-ID`, `X-Forwarded-For` — and
forwards nothing else. An `If-Match` header would be dropped at that boundary
and the guard would silently never fire, which is worse than not having it:
a check that cannot fail reads as protection while providing none. A body
field crosses the BFF unchanged because the BFF forwards the body verbatim.

**Why `updated_at` and not a new `version` column.** Project, Lead, Estimate
and Phase already carry `updated_at` via `UpdatedAtMixin`, whose
`onupdate=utcnow` fires on every ORM UPDATE — exactly the "did this row
change" signal a version counter would provide, already present and already
exposed in each entity's response schema. Adding a `version` column to four
tables would be a migration and a schema-doc divergence buying nothing the
existing column doesn't already give.

**Opt-in, deliberately.** `expected_updated_at` is optional: omit it and the
write proceeds unchecked, exactly as before. Making it mandatory would be the
stronger guarantee and is the right eventual destination, but it is a
breaking API change, so the frontend always sends it and third-party callers
keep working. The guarantee is therefore "a client that opts in cannot
clobber", not "no client can clobber" — worth being precise about rather than
overselling.

**Precision trap, for whoever wires up the next caller.** The comparison is
exact. `updated_at` is microsecond-precision in Postgres and survives the
Pydantic/JSON round trip intact, but JavaScript's `Date` truncates to
milliseconds — so a caller that parses the timestamp into a `Date` and
re-serializes it will send a value that never matches and will 409 forever.
Pass the string through **verbatim**, as received. That failure mode is at
least the safe direction: a spurious conflict blocks a write and asks the
user to reload, where the opposite error would resume silently losing data.
"""
from datetime import datetime
from typing import Protocol

from fastapi import HTTPException, status


class _HasUpdatedAt(Protocol):
    updated_at: datetime


def guard_stale_write(
    entity: _HasUpdatedAt,
    expected_updated_at: datetime | None,
    *,
    entity_name: str,
) -> None:
    """409 if `entity` changed since the caller loaded it. No-op if unchecked.

    Call this immediately after loading the row and **before** the first
    `setattr`, matching the ordering `update_project`'s own comment already
    establishes for its status-transition check: raising before anything is
    staged guarantees nothing from a rejected request reaches the single
    commit `get_current_user` performs after the handler returns.
    """
    if expected_updated_at is None:
        return
    if entity.updated_at != expected_updated_at:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This {entity_name} was changed by someone else after you opened it. "
            "Reload to see their changes, then reapply yours.",
        )
