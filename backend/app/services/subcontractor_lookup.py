"""The by-id chokepoint for reading a Subcontractor.

Moved out of `app/routers/subcontractors.py` for the same reason as
`project_lookup.py`: `bills.py` and `subcontractor_assignments.py` both
imported it across module boundaries by its private name, which CLAUDE.md
forbids and nothing checked. See `project_lookup.py`'s module docstring
for the full rationale and `tests/test_module_boundaries.py` for the gate.

Unlike a Project, a Subcontractor carries no per-role row scope — it is a
company-wide record, and `require_role(...)` at each route already decides
which staff roles may read the surface at all (docs/07's RBAC matrix gives
`field_crew` and `client` no grant on the Compliance row, so neither can
reach a route that calls this). Tenant scope is RLS's job, so this
function is deliberately thin; it exists so the *import direction* is
right, not because it has scoping of its own to protect.
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser
from app.models import Subcontractor


async def get_subcontractor_or_404(
    current: CurrentUser, subcontractor_id: uuid.UUID
) -> Subcontractor:
    """Shared existence/tenant check, same pattern as `_get_estimate_or_404`
    (app/routers/estimates.py) — RLS makes another tenant's subcontractor
    invisible, so this 404 covers both "doesn't exist" and "exists but isn't
    yours" identically (Inherited Invariant #8), intentionally
    indistinguishable from outside. No explicit `company_id` filter in the
    query below — the tenant_isolation RLS policy already does that scoping.
    """
    result = await current.session.execute(
        select(Subcontractor).where(Subcontractor.id == subcontractor_id)
    )
    subcontractor = result.scalar_one_or_none()
    if subcontractor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subcontractor not found")
    return subcontractor
