"""A route-introspection sweep: no timestamped entity's PATCH can quietly
lose its stale-write guard.

`app/services/concurrency.py`'s protection is opt-in at the API level — a
caller that omits `expected_updated_at` gets the old last-write-wins
behaviour, deliberately, so existing clients keep working. That makes today's
callers safe (our frontend always sends it) and says nothing at all about the
*next* PATCH route somebody adds, which would silently ship with no guard.

This is the same problem `test_rls_policy_coverage.py` and
`test_tier_gating.py` already solve for their own invariants, and it gets the
same house answer: sweep every route rather than trusting anyone to remember.
Adding a PATCH route on an entity that has `updated_at` means either wiring
the guard or adding an allowlist entry with a reason — there is no third
option that passes.

The pinned count at the bottom exists for the reason `test_tier_gating.py`
documents at length: a completeness assertion over an accidentally-empty route
list is trivially true. If the introspection breaks, "every route is guarded"
would go quietly green while checking nothing; the count turns that into a
loud failure instead.
"""
import inspect

from app.core.deps import get_current_user  # noqa: F401  (imported for parity with app wiring)
from tests.test_tier_gating import iter_api_routes

# PATCH routes on entities that carry `updated_at` and are therefore guardable.
# Keyed by path so a rename shows up here rather than silently dropping out.
GUARDED_PATCH_ROUTES = {
    "/projects/{project_id}",
    "/leads/{lead_id}",
    "/estimates/{estimate_id}",
    # These three were missed on the first pass and found by this sweep — the
    # reason it exists. Each carries `updated_at` and so was guardable all
    # along; a hand-picked scope of "the obvious three" had simply not looked.
    "/catalogs/items/{item_id}",
    "/materials/{bom_line_id}",
    "/vendors/{vendor_id}",
}

# PATCH routes deliberately NOT guarded, each with the reason it cannot be.
UNGUARDABLE = {
    # `phases` has no created_at/updated_at columns at all
    # (docs/04-database-schema.md Section 4), so there is no value to compare
    # a caller's token against. Guarding it would mean adding a column.
    "/projects/{project_id}/phases/{phase_id}": "phases has no timestamp columns",
    # Same for `tasks`: created_at only, no updated_at, by the schema doc.
    "/tasks/{task_id}": "tasks has no updated_at column",
    # Status transitions are guarded by PROJECT_TRANSITIONS/LEAD_TRANSITIONS
    # instead — an illegal or duplicated transition already 409s, so a
    # concurrent double-click cannot double-advance the record.
    "/projects/{project_id}/status": "guarded by the transition state machine",
    # The remaining four have no `updated_at` column, confirmed against the
    # live schema rather than assumed: there is nothing to compare a token
    # against. Guarding any of them means adding a timestamp column first.
    "/companies/{company_id}": "companies has no updated_at column",
    "/companies/members/{user_id}": "company_users has no updated_at column",
    "/markup-profiles/{profile_id}": "markup_profiles has no timestamp columns",
    "/subcontractors/{subcontractor_id}": "subcontractors has no updated_at column",
}


def _patch_routes():
    from app.main import app

    out = {}
    for route in iter_api_routes(app):
        methods = getattr(route, "methods", None)
        if not methods or "PATCH" not in methods:
            continue
        out[route.path] = route
    return out


def test_every_guardable_patch_route_calls_the_stale_write_guard():
    """The guard has to be *called*, not merely importable. Checked against
    the handler's own source so a route that declares `expected_updated_at`
    and then forgets to act on it still fails."""
    routes = _patch_routes()
    missing = []
    for path in sorted(GUARDED_PATCH_ROUTES):
        route = routes.get(path)
        assert route is not None, (
            f"{path} is in GUARDED_PATCH_ROUTES but no PATCH route has that path — "
            "it was renamed or removed, and its guard coverage went with it."
        )
        source = inspect.getsource(route.endpoint)
        if "guard_stale_write(" not in source:
            missing.append(path)

    assert missing == [], (
        "these PATCH routes are on entities with `updated_at` but never call "
        f"guard_stale_write(), so a concurrent edit silently clobbers: {missing!r}"
    )


def test_every_guarded_route_accepts_the_token_in_its_request_schema():
    """A guard the caller cannot feed is decoration. Asserts the body model
    actually exposes `expected_updated_at`."""
    routes = _patch_routes()
    missing = []
    for path in sorted(GUARDED_PATCH_ROUTES):
        route = routes[path]
        fields = set()
        for field in route.dependant.body_params:
            annotation = field.field_info.annotation
            fields |= set(getattr(annotation, "model_fields", {}))
        if "expected_updated_at" not in fields:
            missing.append(path)

    assert missing == [], (
        "these routes call the guard but their request schema has no "
        f"`expected_updated_at`, so nothing can ever trigger it: {missing!r}"
    )


def test_no_patch_route_is_unclassified():
    """The actual completeness gate: every PATCH route is either guarded or
    explicitly excused. A newly added one belongs to neither set and fails
    here, which is the whole point."""
    routes = _patch_routes()
    classified = GUARDED_PATCH_ROUTES | set(UNGUARDABLE)
    unclassified = sorted(set(routes) - classified)

    assert unclassified == [], (
        f"unclassified PATCH route(s): {unclassified!r}. If the entity has an "
        "`updated_at` column, wire app/services/concurrency.py's "
        "guard_stale_write() and add the path to GUARDED_PATCH_ROUTES. If it "
        "genuinely cannot be guarded, add it to UNGUARDABLE with the reason."
    )


def test_patch_route_count_is_pinned():
    """Guards the sweep itself. `iter_api_routes` walks FastAPI internals
    (`_IncludedRouter.original_router`); if that breaks, `_patch_routes()`
    returns almost nothing and every assertion above passes vacuously. This
    turns that into a failure — the same reasoning, and the same lesson,
    behind test_tier_gating.py's own pinned count."""
    routes = _patch_routes()
    assert len(routes) == 13, (
        f"PATCH route count changed: {len(routes)} (paths: {sorted(routes)!r}). "
        "If a route was genuinely added or removed, update this literal AND "
        "classify it above. If the count collapsed, the introspection broke "
        "and every guard-coverage assertion in this file just stopped checking "
        "anything."
    )
