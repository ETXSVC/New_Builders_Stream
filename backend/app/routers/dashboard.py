"""Dashboard summary counts (CRM+PM frontend spec, Decision 2 item 2).

admin/project_manager only: they are the only roles that see the dashboard
(the frontend redirects field_crew to /my-tasks and client to their project
before this is ever called), and field_crew has no lead access anyway. RLS
scopes every count to the caller's tenant — no explicit company_id filters,
same as every other router.
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.core.deps import CurrentUser, require_role
from app.models import Lead, Project, Task
from app.schemas.dashboard import DashboardSummaryResponse

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_ROLES = ("admin", "project_manager")


@router.get("/summary", response_model=DashboardSummaryResponse)
async def dashboard_summary(
    current: CurrentUser = Depends(require_role(*_ROLES)),
) -> DashboardSummaryResponse:
    today = date.today()
    week_out = today + timedelta(days=7)

    open_leads = await current.session.scalar(
        select(func.count()).select_from(Lead).where(Lead.status.notin_(("won", "lost")))
    )
    active_projects = await current.session.scalar(
        select(func.count()).select_from(Project).where(Project.status == "active")
    )
    tasks_due_this_week = await current.session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.status != "done", Task.due_date.isnot(None), Task.due_date >= today, Task.due_date <= week_out)
    )

    return DashboardSummaryResponse(
        open_leads=open_leads or 0,
        active_projects=active_projects or 0,
        tasks_due_this_week=tasks_due_this_week or 0,
    )
