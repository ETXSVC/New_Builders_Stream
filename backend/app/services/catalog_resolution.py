"""Cost Catalog inheritance resolution (Task 2.4, Phase 2 design decision #1).

`cost_catalog_items` carries the ONLY bidirectional RLS policy in this
codebase (migration 0005). An ordinary RLS-scoped `SELECT * FROM
cost_catalog_items`, run against a session whose `app.current_tenant` GUC is
already set to `active_company_id`, returns every row visible in BOTH
directions:
  - DOWNWARD: the caller's own rows, plus every descendant branch's rows —
    the normal "parent sees its children's data" shape every other table in
    this codebase has.
  - UPWARD (new, specific to this table): every ancestor's rows too — the
    grant that makes US-4.6's "a child branch can see, and override, its
    parent's catalog" possible at all.

What this module does NOT do, and must never do: call `set_current_tenant()`
or otherwise touch tenant/session context in any way.
`resolve_visible_catalog_items` trusts that the caller's session ALREADY has
its RLS context established — normally by `get_current_user`
(`app/core/deps.py`), once, at the start of the request — and relies
entirely on the bidirectional policy above to have already scoped the raw
row set correctly. This is precisely what makes it safe: per Inherited
Invariant #4 / design decision #1, manually re-pointing `app.current_tenant`
mid-request (even temporarily, even if always carefully restored afterward)
is exactly the class of bug this codebase's RLS discipline exists to
prevent — and this module has no need to do it, because the database has
already done 100% of the visibility work by the time
`resolve_visible_catalog_items` receives its first row. No manual
context-switching anywhere in this file, or anywhere else in application
code that calls it.

The only work left at the application layer is dedup-and-prefer-closest: the
raw row set above can contain MULTIPLE rows for the same conceptual catalog
item — an ancestor's original plus one or more descendants' overrides of it,
chained via `parent_catalog_item_id` — and a caller should see exactly ONE
of them: whichever belongs to the company closest to `active_company_id` in
the tree.

Known, documented, accepted limitation for Phase 2 (see
`_root_identity_id`'s docstring below for the mechanics): because
`parent_catalog_item_id` uses `ON DELETE SET NULL`
(`app/models/cost_catalog_item.py`), deleting a row in the MIDDLE of a
multi-level override chain orphans every row still pointing at it. An
orphaned row is treated as a brand-new, independent conceptual identity
(its own root) rather than still being recognized as part of the original
chain — so a resolved list can end up showing what a human would still
consider "the same item" as two separate entries: the original ancestor's
row, and the orphaned descendant's row, standing alone with no override
relationship between them anymore. This is exercised and asserted on
explicitly in
`tests/test_cost_catalog_inheritance.py::test_deleting_middle_override_orphans_grandchilds_override`
— a future phase could special-case re-linking orphaned overrides if this
becomes a real product problem, but nothing in Phase 2's scope calls for it.

Second known, documented, accepted limitation (found during this task's spec
review): "closest wins" only has a unique answer when at least one candidate
in an identity group sits on `active_company_id`'s OWN ancestor chain. When
an ancestor has created no override of its own, and two or more of its
UNRELATED descendant branches (siblings of each other) each independently
override the same inherited item, every candidate clamps to the same
`+inf` hop-distance — see `hop_distance`'s own docstring below — and there
is no principled "closest" among them. Step 3 below breaks this tie
deterministically (most-recently-updated wins, then `id` as a final
tiebreak), rather than leaving it to depend on Postgres' unordered `SELECT`
row-return order. Exercised explicitly in
`tests/test_cost_catalog_inheritance.py::test_diamond_tie_between_unrelated_sibling_overrides_breaks_deterministically`.
"""
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CostCatalogItem


def _compute_hop_distance(
    company_id: uuid.UUID, *, chain_lengths: dict[uuid.UUID, int], active_chain_length: int
) -> float:
    """0 = active_company_id's own row, 1 = its immediate parent's, 2 = its
    grandparent's, etc. A company whose own ancestor-chain is LONGER than
    active_company_id's — i.e. one of active_company_id's own descendants,
    visible here only because of the ordinary downward RLS grant every
    table has, not the new upward one — is never "closer" than
    active_company_id's own row, so it's given an effectively infinite
    distance rather than a meaningless negative one. This is what
    guarantees a company's own row always wins over any descendant's
    override of the same item (US-4.6's "the parent catalog itself is
    unaffected" acceptance criterion): 0 is always the smallest possible
    distance, and active_company_id's own row (if present in a group)
    always has distance 0.

    Extracted as a top-level function (rather than a closure inside
    `resolve_visible_catalog_items`), matching this codebase's own
    `is_legal_transition()`-style precedent (`app/services/lead_transitions.py`)
    for small, pure, independently-unit-testable logic — see
    `tests/test_cost_catalog_inheritance.py`'s fast, DB-free unit tests for
    this function specifically.
    """
    distance = active_chain_length - chain_lengths[company_id]
    return distance if distance >= 0 else float("inf")


def _root_identity_id(item: CostCatalogItem, *, items_by_id: dict[uuid.UUID, CostCatalogItem]) -> uuid.UUID:
    """Walks `item`'s override chain up to its root and returns that root
    row's id as the conceptual identity key for grouping.

    If a row's `parent_catalog_item_id` points at an id that isn't in
    `items_by_id` — either because it's genuinely NULL (an original,
    never-overridden item), or because the row it used to point to was
    deleted and `ON DELETE SET NULL` fired (see this module's docstring for
    the accepted Phase 2 limitation this produces) — the walk stops there
    and `item` itself (or whichever row it reached before hitting the gap)
    becomes its own root. This is a deliberate, documented choice, not an
    oversight: it never raises and never drops a row, it just means an
    orphaned override becomes a new, independent identity rather than
    staying linked to its original ancestor.

    The `seen` cycle guard defends against a `parent_catalog_item_id` loop
    that no current write path can actually create (nothing in this
    codebase yet validates against it at write time) — kept as a defensive
    backstop so a future bug elsewhere degrades to "wrong grouping" rather
    than an infinite loop. See this function's own unit tests for direct,
    DB-free coverage of both the normal walk and this guard.

    Extracted as a top-level function for the same reason as
    `_compute_hop_distance` above — see its docstring.
    """
    current = item
    seen: set[uuid.UUID] = set()
    while current.parent_catalog_item_id is not None and current.id not in seen:
        seen.add(current.id)
        parent = items_by_id.get(current.parent_catalog_item_id)
        if parent is None:
            break
        current = parent
    return current.id


async def resolve_visible_catalog_items(
    session: AsyncSession, active_company_id: uuid.UUID
) -> list[CostCatalogItem]:
    """Returns `active_company_id`'s effective Cost Catalog: exactly one row
    per conceptual item, preferring whichever override is closest to
    `active_company_id` in the company hierarchy (0 hops = the caller's own
    row, 1 hop = its immediate parent's, 2 hops = its grandparent's, etc.).

    Deliberately does NOT call `set_current_tenant()` / `set_current_user()`
    or read/write any tenant-context GUC — see this module's docstring for
    why relying entirely on the session's already-established RLS context
    (set once, upstream, by `get_current_user`) is what makes this function
    safe to call from anywhere without re-deriving that discipline.
    """
    result = await session.execute(select(CostCatalogItem))
    visible_items = list(result.scalars().all())
    if not visible_items:
        return []

    # --- Step 1: hop-distance from active_company_id to every OTHER
    # company that owns at least one visible row. ------------------------
    #
    # `companies` itself carries the ORDINARY (descendant-only) RLS policy,
    # not the bidirectional one cost_catalog_items has — a session scoped to
    # a child company cannot SELECT its parent's `companies` row directly.
    # So a naive Python walk of `Company.parent_id` one level at a time
    # (`SELECT parent_id FROM companies WHERE id = ...`, repeated) would
    # silently dead-end after the caller's own row: the very next SELECT,
    # for the parent's row, would come back empty even though the row
    # exists, because it isn't visible to this session under `companies`'
    # own policy. The only thing in this codebase that can see an ancestor
    # chain regardless of `companies`' ordinary RLS is
    # `get_all_ancestor_ids()` itself (migration 0005's SECURITY DEFINER
    # function, EXECUTE granted to app_user) — called here directly, the
    # same function the bidirectional policy itself calls.
    #
    # `get_all_ancestor_ids(x)` returns an unordered SET of ids (x plus
    # every ancestor up to the root), not a depth/level column, so a hop
    # count can't be read off it directly. But every company has AT MOST
    # ONE parent (`companies.parent_id` is a single nullable self-FK), so
    # the ancestor chain is always a simple linked list, never a branching
    # tree — which means the SIZE of `get_all_ancestor_ids(x)`'s result is
    # exactly x's own distance-to-root, plus one. Calling the function once
    # per distinct company_id actually present among the visible rows
    # (never once per row, and never against the whole `companies` table)
    # and comparing chain LENGTHS is therefore enough to derive every
    # relevant company's hop-distance from `active_company_id`, without a
    # bespoke depth-returning SQL function and without a row-by-row walk
    # that ordinary RLS would silently truncate.
    distinct_company_ids = {item.company_id for item in visible_items}
    distinct_company_ids.add(active_company_id)

    chain_lengths: dict[uuid.UUID, int] = {}
    for company_id in distinct_company_ids:
        chain_result = await session.execute(
            text("SELECT id FROM get_all_ancestor_ids(:company_id)"),
            {"company_id": str(company_id)},
        )
        chain_lengths[company_id] = len(chain_result.fetchall())

    active_chain_length = chain_lengths[active_company_id]

    # --- Step 2: group rows by conceptual item identity. ------------------
    #
    # Walked via `parent_catalog_item_id`, using only the already-fetched
    # `visible_items` (no extra queries) — the root of a chain, or the row
    # itself if it has no parent link, is the identity key.
    items_by_id = {item.id: item for item in visible_items}

    groups: dict[uuid.UUID, list[CostCatalogItem]] = {}
    for item in visible_items:
        groups.setdefault(_root_identity_id(item, items_by_id=items_by_id), []).append(item)

    # --- Step 3: within each identity group, keep only the closest row. ---
    #
    # Tie-break, found during this task's spec review: in every case
    # exercised by this task's own write paths so far, an identity group
    # never contains two rows owned by the SAME company, so at most one
    # candidate has a given FINITE hop-distance, and min() alone would be
    # unambiguous whenever active_company_id or one of its own ancestors
    # has a row in the group. (Nothing in the schema actually forbids two
    # same-company rows in one group — there's no CHECK constraint tying a
    # child row's company_id to differ from its parent row's — so this
    # isn't a structural guarantee, just an accurate description of every
    # write path Phase 2 defines. The composite key below resolves ties at
    # ANY distance, not only +inf, so this doesn't rely on the assumption
    # holding.)
    #
    # Every candidate NOT on active_company_id's own ancestor chain (e.g. a
    # sibling branch's override, visible only via the ordinary downward
    # grant on some OTHER ancestor) clamps to the same +inf. This is
    # genuinely reachable, not hypothetical: an ancestor with no override
    # of its own, whose two unrelated descendant branches each
    # independently override the same inherited item, resolving from that
    # ancestor's own point of view. Without a secondary key, Python's min()
    # would silently pick whichever row Postgres' unordered SELECT happened
    # to return first — non-deterministic in practice, since nothing in
    # this function issues an ORDER BY.
    #
    # (updated_at DESC, id ASC) breaks the tie deterministically: the most
    # recently touched candidate wins (a plausible, if not spec-mandated,
    # proxy for "the override someone most recently and deliberately
    # chose"), with `id` as a final tiebreak — not because raw UUID order
    # carries any meaning (every id here comes from uuid.uuid4(), random,
    # not time-ordered), only so two rows sharing an identical timestamp
    # still resolve the same way on every call. This only ever activates
    # among same-hop-distance (usually +inf) rows — it never overrides
    # hop_distance's own ordering, since Python's sort is stable and the
    # primary key (hop_distance) is compared first.
    def _sort_key(it: CostCatalogItem) -> tuple[float, float, uuid.UUID]:
        distance = _compute_hop_distance(
            it.company_id, chain_lengths=chain_lengths, active_chain_length=active_chain_length
        )
        return (distance, -it.updated_at.timestamp(), it.id)

    return [min(group_items, key=_sort_key) for group_items in groups.values()]
