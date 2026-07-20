from pydantic import BaseModel


class DashboardSummaryResponse(BaseModel):
    """GET /dashboard/summary (CRM+PM frontend spec, Decision 2 item 2).
    Exact COUNTs, not page-derived approximations — the list endpoints are
    cursor-paginated with no total field."""

    open_leads: int
    active_projects: int
    tasks_due_this_week: int
