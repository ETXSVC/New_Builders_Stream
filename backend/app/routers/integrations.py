"""Task 4.7 (design spec Section 3): GET /integrations/{provider}/connect.
Top-level routes, not company-nested — provider is a path segment
(quickbooks/freshbooks), matching the API spec's own sketch. RBAC per
docs/07-security-compliance.md Section 2's "Integrations" row:
Admin/Accountant only, ZERO Client/PM/Field-Crew access.
"""
from typing import Literal

from fastapi import APIRouter, Depends

from app.core.deps import CurrentUser, require_role
from app.schemas.integration import AuthorizationUrlResponse
from app.services.accounting_client import get_accounting_client
from app.services.integration_oauth_state import sign_oauth_state

router = APIRouter(prefix="/integrations", tags=["integrations"])

_ROLES = ("admin", "accountant")

Provider = Literal["quickbooks", "freshbooks"]


@router.get("/{provider}/connect", response_model=AuthorizationUrlResponse)
async def connect(
    provider: Provider,
    current: CurrentUser = Depends(require_role(*_ROLES)),
) -> AuthorizationUrlResponse:
    state = sign_oauth_state(company_id=str(current.company_id), provider=provider)
    client = get_accounting_client(provider)
    authorization_url = await client.get_authorization_url(
        company_id=str(current.company_id), state=state
    )
    return AuthorizationUrlResponse(authorization_url=authorization_url)
