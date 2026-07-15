"""Task 4.7 (design spec Section 3): GET /integrations/{provider}/connect.
Top-level routes, not company-nested — provider is a path segment
(quickbooks/freshbooks), matching the API spec's own sketch. RBAC per
docs/07-security-compliance.md Section 2's "Integrations" row:
Admin/Accountant only, ZERO Client/PM/Field-Crew access.
"""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.deps import CurrentUser, require_role
from app.models import IntegrationConnection
from app.schemas.integration import AuthorizationUrlResponse
from app.services.accounting_client import get_accounting_client
from app.services.integration_oauth_state import sign_oauth_state
from app.services.token_encryption import encrypt_token

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


async def _upsert_connection(
    current: CurrentUser, *, company_id: uuid.UUID, provider: str, access_token: str, refresh_token: str
) -> IntegrationConnection:
    """Reconnecting the same provider (callback firing again) replaces
    stale tokens rather than erroring — design spec Section 1's own
    ON CONFLICT (company_id, provider) DO UPDATE requirement. Uses
    Postgres's native upsert (not a SELECT-then-INSERT-or-UPDATE) so this
    is a single atomic statement, not a race between two separate
    connect attempts for the same company+provider landing concurrently."""
    stmt = (
        pg_insert(IntegrationConnection)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            provider=provider,
            access_token_encrypted=encrypt_token(access_token),
            refresh_token_encrypted=encrypt_token(refresh_token),
        )
        .on_conflict_do_update(
            index_elements=["company_id", "provider"],
            set_={
                "access_token_encrypted": encrypt_token(access_token),
                "refresh_token_encrypted": encrypt_token(refresh_token),
            },
        )
        .returning(IntegrationConnection)
    )
    result = await current.session.execute(stmt)
    await current.session.flush()
    return result.scalar_one()
