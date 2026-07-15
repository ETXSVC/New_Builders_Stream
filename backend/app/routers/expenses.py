"""Task 3.44 (design spec Section 6): POST/GET /projects/{id}/expenses.
Unchanged in shape from the original pre-sketch — no lifecycle, no vendor
link, RBAC matches the same Admin/Accountant-only "Accounting/Billing" row
Bills use (docs/07-security-compliance.md Section 2).
"""
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.events import publish
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.tier_gating import require_module
from app.models import Expense
from app.routers.projects import _get_project_or_404
from app.schemas.expense import ExpenseCreateRequest, ExpenseListResponse, ExpenseResponse

router = APIRouter(tags=["expenses"])

_ROLES = ("admin", "accountant")


@router.post(
    "/projects/{project_id}/expenses",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_expense(
    project_id: uuid.UUID,
    body: ExpenseCreateRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("accounting")),
) -> ExpenseResponse:
    project = await _get_project_or_404(current, project_id)

    expense = Expense(
        id=uuid.uuid4(),
        project_id=project.id,
        company_id=project.company_id,
        description=body.description,
        amount=body.amount,
        incurred_on=body.incurred_on,
    )
    current.session.add(expense)
    await current.session.flush()
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns.

    await publish(
        "EXPENSE_CREATED",
        session=current.session,
        entity_type="expense",
        entity_id=expense.id,
        company_id=expense.company_id,
    )

    return ExpenseResponse.model_validate(expense)


@router.get("/projects/{project_id}/expenses", response_model=ExpenseListResponse)
async def list_expenses(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ExpenseListResponse:
    project = await _get_project_or_404(current, project_id)

    query = select(Expense).where(Expense.project_id == project.id)
    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Expense.incurred_on,
        id_col=Expense.id,
        cursor=cursor,
        limit=limit,
    )

    items = [ExpenseResponse.model_validate(row) for row in rows]
    return ExpenseListResponse(items=items, next_cursor=next_cursor)
