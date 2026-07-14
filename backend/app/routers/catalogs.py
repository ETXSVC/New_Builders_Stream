"""Task 2.5: `POST/GET /catalogs/items`, `POST/GET /markup-profiles`.

Neither list route below reuses `app/core/pagination.py`'s `paginate()`
directly, unlike every other Phase 1 list route — `GET /catalogs/items`
pages over an already-materialized Python list rather than a `Select`
(see `_paginate_resolved_items`'s docstring), and `GET /markup-profiles`
pages over a table with no `created_at`/`updated_at` column for
`paginate()`'s hardcoded cursor to order on (see `_paginate_markup_profiles`'s
docstring). Both helpers keep `paginate()`'s limit+1-fetch-and-trim
discipline; see each one for the full reasoning.
"""

import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, InvalidCursorError, decode_cursor, encode_cursor
from app.models import CostCatalogItem, MarkupProfile
from app.schemas.cost_catalog_item import (
    CostCatalogItemCreateRequest,
    CostCatalogItemListResponse,
    CostCatalogItemResponse,
)
from app.schemas.markup_profile import (
    MarkupProfileCreateRequest,
    MarkupProfileListResponse,
    MarkupProfileResponse,
)
from app.services.catalog_resolution import resolve_visible_catalog_items

router = APIRouter(tags=["catalogs"])

# docs/07-security-compliance.md Section 2's RBAC matrix, Estimation row:
# "Full CRUD" for Admin and PM, "Read" for Accountant, "—" (nothing) for
# Field Crew, and Client only gets the estimate-specific "Approve/reject own
# estimate (e-sign)" grant — which is scoped to `estimates`, not Cost
# Catalog/Markup Profile data, so `client` is excluded from both tuples
# below, same reasoning `_LIST_ROLES`/`_GET_ROLES` in projects.py give for
# excluding roles the matrix doesn't actually grant.
_WRITE_ROLES = ("admin", "project_manager")
_READ_ROLES = ("admin", "project_manager", "accountant")


# =============================================================================
# Cost Catalog Items
# =============================================================================


@router.post(
    "/catalogs/items",
    response_model=CostCatalogItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_catalog_item(
    payload: CostCatalogItemCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> CostCatalogItemResponse:
    """Plain "brand-new catalog item" create — no `parent_catalog_item_id`
    anywhere in this route or its request schema (see
    `CostCatalogItemCreateRequest`'s own docstring). Creating an OVERRIDE of
    a visible ancestor's item is a structurally distinct operation, routed
    through `create_catalog_item_override` below instead of a
    `parent_catalog_item_id` field accepted here — see that route's
    docstring for the full reasoning behind splitting these into two routes
    rather than one route branching on an optional body field.
    """
    item = CostCatalogItem(
        company_id=current.company_id,
        parent_catalog_item_id=None,
        category=payload.category,
        name=payload.name,
        unit=payload.unit,
        unit_rate=payload.unit_rate,
    )
    current.session.add(item)
    await current.session.flush()
    # No audit_log entry: Cost Catalog item creation isn't in
    # docs/07-security-compliance.md Section 5's enumerated list of
    # financially/legally significant state changes needing an audit trail
    # (that list is status transitions/approvals/overrides on
    # Projects/Estimates) — same "not every create needs an audit entry"
    # precedent Phase 1 already established for Project/Phase/Task/Document
    # creation (see projects.py's create_project docstring). No explicit
    # commit either — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns.

    return CostCatalogItemResponse.model_validate(item)


@router.post(
    "/catalogs/items/{parent_catalog_item_id}/override",
    response_model=CostCatalogItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_catalog_item_override(
    parent_catalog_item_id: uuid.UUID,
    payload: CostCatalogItemCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> CostCatalogItemResponse:
    """A distinct route, not a `parent_catalog_item_id` field accepted by
    `create_catalog_item` above — matching `CostCatalogItemCreateRequest`'s
    own docstring ("Task 2.4/2.5 define the override-specific routing... a
    separate override endpoint/flow... on top of this plain create"). Two
    reasons this reads more cleanly as its own route than as a branch on an
    optional body field:
      1. The two operations have genuinely different validation stories —
         a plain create has none beyond the body's own field constraints,
         while an override MUST first check that `parent_catalog_item_id`
         is actually visible to the caller (see below). Keeping them as one
         route with an `if payload.parent_catalog_item_id is not None:`
         branch would smear that extra validation across a single handler
         body instead of making it the entire point of a dedicated one.
      2. `parent_catalog_item_id` reads naturally as a PATH parameter
         (identifying "the ancestor item you are overriding") rather than a
         body field, the same way `/projects/{project_id}/documents`
         addresses the project being uploaded to via the path, not a
         `project_id` field duplicated into the body.

    Visibility check: `parent_catalog_item_id` must be visible to the caller
    via `resolve_visible_catalog_items` — checked directly against ITS
    output, not a raw RLS-scoped query. The bidirectional RLS policy
    (migration 0005) alone does NOT stop a caller from attempting to
    override an ancestor item id it can technically SELECT: `WITH CHECK`
    only constrains the new row's own `company_id` (must be the caller's own
    descendant set), never the FK TARGET's visibility, so nothing at the
    database layer stops a well-formed INSERT pointing
    `parent_catalog_item_id` at, say, a sibling branch's item id that
    happens to be reachable via the same ordinary downward RLS grant every
    table has. Checking against `resolve_visible_catalog_items`' resolved
    output (not the raw RLS-visible set) additionally enforces the intended
    override semantics for multi-level chains: a grandchild overriding an
    item its own parent has ALREADY overridden must supply the PARENT's
    override id (the grandchild's actual resolved/closest view of that
    item), not the original grandparent id — supplying the grandparent's id
    here would 404, because it no longer appears in the grandchild's own
    resolved list (see
    `tests/test_cost_catalog_inheritance.py::test_grandchild_overriding_parents_override_sees_closest_not_grandparents_original`
    for the same resolution behavior this check is built on).

    404, not 403, on an invisible/nonexistent parent id — Inherited
    Invariant #8: "doesn't exist" and "exists but isn't visible to you" are
    intentionally indistinguishable from outside, same as every other 404 in
    this codebase.
    """
    resolved_items = await resolve_visible_catalog_items(current.session, current.company_id)
    resolved_ids = {item.id for item in resolved_items}
    if parent_catalog_item_id not in resolved_ids:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Catalog item not found")

    item = CostCatalogItem(
        company_id=current.company_id,
        parent_catalog_item_id=parent_catalog_item_id,
        category=payload.category,
        name=payload.name,
        unit=payload.unit,
        unit_rate=payload.unit_rate,
    )
    current.session.add(item)
    await current.session.flush()
    # No audit_log entry, same reasoning as create_catalog_item above. No
    # explicit commit — Inherited Invariant #4.

    return CostCatalogItemResponse.model_validate(item)


def _paginate_resolved_items(
    items: list[CostCatalogItem], *, cursor: str | None, limit: int
) -> tuple[list[CostCatalogItem], str | None]:
    """The in-memory equivalent of `app/core/pagination.py`'s `paginate()`,
    for the one list-shaped (not query-shaped) case in this codebase — see
    this module's own top-of-file docstring for why `paginate()` itself
    cannot be called here at all (there's no `Select` left after
    `resolve_visible_catalog_items` has already materialized and deduped
    the row set).

    **Sort order.** `resolve_visible_catalog_items` returns dict-values
    order (`groups.values()` in `app/services/catalog_resolution.py`), which
    has no guaranteed stability across calls — Python dicts preserve
    INSERTION order, not any semantic order, and insertion order here is
    itself just whatever order Postgres' unordered `SELECT` happened to
    return rows in. Paginating over that directly would risk showing the
    same item twice or skipping one across two page requests, exactly the
    failure mode `paginate()`'s own module docstring identifies for offset
    pagination generally. An explicit, deterministic sort is required before
    cursor pagination has any meaning at all — this uses `(updated_at, id)`,
    ascending, the SAME composite-tiebreak shape (timestamp, then id) every
    other paginated list route in this codebase already uses via
    `paginate()`, just substituting `updated_at` for `created_at` since
    `CostCatalogItem` has no `created_at` column (`app/models/cost_catalog_item.py`
    — UpdatedAtMixin only, matching the schema doc). This was chosen over an
    alternative "alphabetical by category then name" sort: staying
    consistent with the (timestamp, id) shape already established elsewhere
    in this codebase means `encode_cursor`/`decode_cursor`
    (`app/core/pagination.py`) can be reused completely as-is below — their
    signature (`datetime`, `uuid.UUID`) never actually references
    "created_at" by name, it just serializes an opaque `(timestamp, id)`
    pair, so passing `updated_at` in is a legitimate reuse, not a hack.
    Reusing them also means every cursor this API ever hands back — across
    every list route — decodes the same way, which a bespoke
    business-field cursor scheme would have broken.

    **Memory tradeoff, accepted deliberately.** This re-sorts (and, when a
    cursor is present, re-filters) the ENTIRE resolved list on every page
    request, rather than touching only the requested page's worth of rows
    the way `paginate()`'s SQL-level `LIMIT` does. That's acceptable here
    because `resolve_visible_catalog_items` already loads the full visible
    row set into memory unconditionally on every call — its dedup/prefer-closest
    logic must see every candidate in an identity group before it can pick
    the closest one, so there is no pagination strategy that avoids that
    cost once it has been called. This function doesn't add a second,
    larger cost on top of that.
    """
    ordered = sorted(items, key=lambda item: (item.updated_at, item.id))

    if cursor is not None:
        cursor_updated_at, cursor_id = decode_cursor(cursor)
        ordered = [
            item for item in ordered if (item.updated_at, item.id) > (cursor_updated_at, cursor_id)
        ]

    # Same limit+1-fetch-and-trim trick paginate() uses, just against a
    # Python list slice instead of a SQL LIMIT — fetch one extra item to
    # learn whether a next page exists without a second pass.
    page = ordered[: limit + 1]
    next_cursor: str | None = None
    if len(page) > limit:
        page = page[:limit]
        last = page[-1]
        next_cursor = encode_cursor(last.updated_at, last.id)

    return page, next_cursor


@router.get("/catalogs/items", response_model=CostCatalogItemListResponse)
async def list_catalog_items(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    category: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> CostCatalogItemListResponse:
    """Returns `resolve_visible_catalog_items`'s deduped, prefer-closest
    output — deliberately NOT a raw RLS-scoped `SELECT * FROM
    cost_catalog_items`. A raw query would return BOTH an ancestor's
    original item AND a descendant branch's override of that same item as
    two separate rows (the bidirectional RLS policy makes both visible),
    directly contradicting US-4.6's "a child branch's local override takes
    precedence over the parent's catalog entry for that item" — a caller
    would see duplicate-looking entries for what should read as one
    conceptual item.

    `category`/`search` (docs/05-api-specification.md Section 5: "Pagination,
    category filter, search") are applied AFTER resolution, against the
    already-deduped list — not as a WHERE clause on some earlier raw query.
    Filtering before resolution would be actively wrong: an ancestor's
    original item might match a filter while a descendant's OVERRIDE of that
    same item (different name/category, that's the whole point of an
    override) does not, or vice versa, and it's the caller's actually-visible
    RESOLVED version of an item that should be tested against the filter,
    never a since-superseded ancestor's row that the caller wouldn't
    otherwise see at all.

    `category`: exact match, same convention `status_filter` uses in
    `leads.py`/`projects.py` (a controlled-ish vocabulary field, not free
    text). `search`: case-insensitive substring match against `name` — the
    literal field a caller is realistically searching a catalog by; the API
    spec names "search" without specifying which field(s), and `name` is the
    obvious candidate (`category` already has its own dedicated exact-match
    filter immediately above it in the spec's own Key Inputs column).
    """
    resolved_items = await resolve_visible_catalog_items(current.session, current.company_id)

    if category is not None:
        resolved_items = [item for item in resolved_items if item.category == category]
    if search is not None:
        needle = search.lower()
        resolved_items = [item for item in resolved_items if needle in item.name.lower()]

    try:
        page, next_cursor = _paginate_resolved_items(resolved_items, cursor=cursor, limit=limit)
    except InvalidCursorError:
        # Not caught here on purpose beyond this comment: app/main.py already
        # registers a global InvalidCursorError -> 400 handler
        # (invalid_cursor_handler), same one paginate()'s own callers rely
        # on. Re-raising (implicitly, by not catching) keeps this route
        # consistent with every other paginated list route in the codebase.
        raise

    return CostCatalogItemListResponse(
        items=[CostCatalogItemResponse.model_validate(item) for item in page],
        next_cursor=next_cursor,
    )


# =============================================================================
# Markup Profiles
# =============================================================================


@router.post(
    "/markup-profiles",
    response_model=MarkupProfileResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_markup_profile(
    payload: MarkupProfileCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> MarkupProfileResponse:
    """Plain company-scoped create, no inheritance concept at all — design
    decision #1's closing note and `MarkupProfile`'s own model docstring:
    `markup_profiles` has no `parent_profile_id` column, unlike
    `cost_catalog_items`."""
    profile = MarkupProfile(
        company_id=current.company_id,
        name=payload.name,
        overhead_pct=payload.overhead_pct,
        profit_pct=payload.profit_pct,
    )
    current.session.add(profile)
    await current.session.flush()
    # No audit_log entry (same reasoning as create_catalog_item above). No
    # explicit commit — Inherited Invariant #4.

    return MarkupProfileResponse.model_validate(profile)


def _encode_id_cursor(id_: uuid.UUID) -> str:
    """A bare-id cursor, deliberately not `app/core/pagination.py`'s
    `encode_cursor` — that function's on-the-wire FORMAT (base64 of
    `"{timestamp}|{id}"`) is fine to reuse generically (see
    `_paginate_resolved_items` above), but its SIGNATURE requires a
    `datetime` positional argument that `markup_profiles` simply has nothing
    to supply (no `created_at`/`updated_at` column exists on this table at
    all — see this module's top-of-file docstring). Rather than pass a fake
    placeholder datetime through `encode_cursor` to force-fit its shape,
    this is its own tiny, honestly-single-column cursor: opaque
    base64(str(id)), same "callers pass it back verbatim, never construct or
    parse it themselves" API-stability rationale `encode_cursor`'s own
    docstring gives, just for one field instead of two."""
    return urlsafe_b64encode(str(id_).encode("ascii")).decode("ascii")


def _decode_id_cursor(cursor: str) -> uuid.UUID:
    try:
        return uuid.UUID(urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except Exception as exc:  # noqa: BLE001 - deliberately broad, mirroring
        # decode_cursor's own reasoning in app/core/pagination.py: any
        # malformed input collapses to the same InvalidCursorError, which
        # app/main.py's global exception handler already turns into a 400
        # for every paginated route, this one included.
        raise InvalidCursorError("Invalid pagination cursor") from exc


async def _paginate_markup_profiles(
    session: AsyncSession,
    query: Select,
    *,
    id_col: InstrumentedAttribute[uuid.UUID],
    cursor: str | None,
    limit: int,
) -> tuple[list[MarkupProfile], str | None]:
    """SQL-level cursor pagination, same limit+1-fetch-and-trim discipline as
    `app/core/pagination.py`'s `paginate()` — but ordered on `id` ALONE, not
    the `(created_at, id)` composite `paginate()` hardcodes. `markup_profiles`
    has neither a `created_at` nor an `updated_at` column
    (docs/04-database-schema.md Section 5 / `app/models/markup_profile.py`'s
    own docstring, a deliberate Task 2.1 decision, not an oversight this
    task should silently work around by adding a column the schema doc never
    specified) — so `paginate()` cannot be called against this table at all,
    its signature requires a timestamp column that structurally does not
    exist here.

    `id` alone is still a structurally sound cursor key despite carrying no
    calendar meaning: `app/core/pagination.py`'s own module docstring
    already establishes that `id` (UUID) "carries no ordering meaning of its
    own, but it is unique and immutable, which is all a tiebreaker needs to
    be: it guarantees a strict total order over the table" — the exact
    property cursor pagination's correctness (no dupes/skips across pages)
    actually depends on. Every `MarkupProfile.id` comes from `uuid.uuid4()`
    (random, not time-ordered, `UUIDPKMixin`), so `ORDER BY id` produces a
    stable but not human-meaningful order — an accepted tradeoff, not a
    defect: a caller paging through markup profiles sees every profile
    exactly once across pages, just not in creation or alphabetical order.
    """
    if cursor is not None:
        cursor_id = _decode_id_cursor(cursor)
        query = query.where(id_col > cursor_id)

    query = query.order_by(id_col.asc()).limit(limit + 1)

    result = await session.execute(query)
    rows = list(result.scalars().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_id_cursor(getattr(rows[-1], id_col.key))

    return rows, next_cursor


@router.get("/markup-profiles", response_model=MarkupProfileListResponse)
async def list_markup_profiles(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> MarkupProfileListResponse:
    """Plain company-scoped list — no inheritance concept, unlike
    `list_catalog_items` above (design decision #1's closing note). No
    explicit `company_id` filter needed: `markup_profiles`' ordinary
    (non-bidirectional) `tenant_isolation` RLS policy already scopes every
    row this query can see to the caller's active tenant, same pattern
    `list_projects`/`list_leads` rely on.
    """
    query = select(MarkupProfile)

    rows, next_cursor = await _paginate_markup_profiles(
        current.session,
        query,
        id_col=MarkupProfile.id,
        cursor=cursor,
        limit=limit,
    )

    return MarkupProfileListResponse(
        items=[MarkupProfileResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )
