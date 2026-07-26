"""The one by-id chokepoint for reading a Project, and the field_crew
visibility predicate it shares with `list_projects`.

This lived in `app/routers/projects.py` as `_get_project_or_404`, and six
other routers reached across module boundaries to import it by its private
name — `bills`, `expenses`, `invoices`, `tasks`, `change_orders` and
`subcontractor_assignments` all did `from app.routers.projects import
_get_project_or_404`. CLAUDE.md's rule is that a module reaches another
module's data through `app/services/`, never by importing its internals;
that rule had no enforcement, so the underscore stopped meaning anything
and the router became a de facto library for half the app.

Moving it here is not cosmetic. Everything a caller must not skip is
folded into one function:

  * **tenant scope** comes from RLS, so the 404 below covers "no such
    project" and "someone else's project" identically (Inherited Invariant
    #8) — deliberately indistinguishable from outside;
  * **field_crew's assigned-only scope** is applied to the by-id read, not
    just the list route, so a field_crew user asking for a project they
    hold no task on gets the same 404 as for a nonexistent one;
  * **the `client` role's row scope** (migration 0019) runs on every
    by-id read, which is what stops one customer of a company from
    reading another customer's project by guessing an id.

A router that hand-rolls `select(Project).where(Project.id == ...)`
silently opts out of the last two. That is exactly what
`app/routers/bom_lines.py` did — it carried its own four-line copy with
neither scope, harmless only because its routes happen to be
admin/project_manager-only, and a latent hole the moment that tuple
grows. It now calls this instead.

`tests/test_module_boundaries.py` is what keeps the import direction from
regressing.
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser
from app.models import Phase, Project, Task
from app.services.client_scope import require_client_access_to_project


def with_field_crew_scope(query, current: CurrentUser):
    """Field crew's assigned-only visibility predicate: a project qualifies
    if ANY of its tasks, through ANY of its phases, is assigned to this
    user. Shared by `list_projects` and `get_project_or_404` so the two
    enforcement points can't drift apart — this is RBAC-enforcement logic
    (docs/07-security-compliance.md Section 2: Field Crew gets "Read
    assigned" for Project Management, an unqualified statement covering
    both list and single-item read), so a future change to the predicate
    (task status filtering, reassignment handling, etc.) only needs to
    happen once. Expressed as a correlated EXISTS rather than a JOIN so a
    field_crew user with multiple matching tasks on the same project
    doesn't get duplicate rows for it.
    """
    assigned_task_exists = (
        select(Task.id)
        .join(Phase, Phase.id == Task.phase_id)
        .where(Phase.project_id == Project.id, Task.assignee_id == current.user.id)
        .exists()
    )
    return query.where(assigned_task_exists)


async def get_project_or_404(current: CurrentUser, project_id: uuid.UUID) -> Project:
    """Shared existence/tenant/RBAC-scope check, same pattern as leads.py's
    `_get_lead_or_404` — RLS makes another tenant's project invisible, so
    this 404 covers "doesn't exist" and "exists but isn't yours"
    identically, intentionally indistinguishable from outside.

    Also enforces field_crew's assigned-only read scope
    (`with_field_crew_scope`) here, not just on the list route — a
    field_crew user requesting a project they have no task on gets the
    same 404 as a genuinely nonexistent/cross-tenant one, for the same
    information-disclosure reason every other 404 in this codebase is
    existence-indistinguishable. Folded into the initial query (rather
    than a separate EXISTS round trip after fetching the row) so this is
    one query, not two.
    """
    query = select(Project).where(Project.id == project_id)
    if current.role == "field_crew":
        query = with_field_crew_scope(query, current)

    result = await current.session.execute(query)
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    # `client`'s equivalent of field_crew's assigned-only scope above
    # (migration 0019). `GET /projects/{id}` doubles as the client dashboard
    # (design decision #8), and before this a client could read the
    # dashboard of any project in the company by id. A separate round trip
    # rather than folded into the query: the membership tables are a
    # different shape from field_crew's task-assignment EXISTS, and
    # `client_scope` keeps that rule in one place for every router.
    await require_client_access_to_project(current, project.id)

    return project
