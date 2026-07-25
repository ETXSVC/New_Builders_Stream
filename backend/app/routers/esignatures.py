"""Task 2.18: `GET /esignatures/{id}`.

This new router file is required because `GET /esignatures/{id}`
(docs/05-api-specification.md Section 5: "Retrieve signature record
(audit)") is a standalone, `document_type`-agnostic resource route with no
existing home — it doesn't belong in `estimates.py` or a future
`change_orders.py` router since an `Esignature` isn't owned by either one
specifically (it's referenced FROM both, via `document_type`).

There is deliberately no `POST /esignatures` route here (or anywhere else):
docs/05-api-specification.md Section 5 lists only `GET /esignatures/{id}`
for this resource. Capture happens via `capture_esignature`
(`app/services/esignature.py`), called directly by the future Estimate
approval (Task 2.19) and Change Order approval (Task 2.22) endpoints — not
through a dedicated create route this task would otherwise need to add.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.core.deps import CurrentUser, require_role
from app.models import Esignature
from app.schemas.esignature import EsignatureResponse

router = APIRouter(prefix="/esignatures", tags=["esignatures"])

# docs/07-security-compliance.md Section 2's RBAC matrix, Estimation row:
# "Full CRUD" for Admin/PM, "Read" for Accountant, and Client's
# "Approve/reject own estimate (e-sign)" grant — which this router reads,
# per resolved judgment call #4, as implying read access to the resulting
# signature record too (the same client who approved/e-signed an estimate
# is exactly who needs to be able to retrieve the audit record of their own
# signature). Matches `estimates.py`'s own `_READ_ROLES` shape exactly.
# Field Crew gets nothing on this row and is absent.
#
# No `_WRITE_ROLES` tuple here — there is no create/write ROUTE in this
# router at all (module docstring above).
_READ_ROLES = ("admin", "project_manager", "accountant", "client")


async def _get_esignature_or_404(current: CurrentUser, esignature_id: uuid.UUID) -> Esignature:
    """Shared existence/tenant check, mirroring `_get_estimate_or_404`'s
    exact shape (`app/routers/estimates.py`) — RLS makes another tenant's
    esignature invisible, so this 404 covers both "doesn't exist" and
    "exists but isn't yours" identically (Inherited Invariant #8),
    intentionally indistinguishable from outside."""
    result = await current.session.execute(select(Esignature).where(Esignature.id == esignature_id))
    esignature = result.scalar_one_or_none()
    if esignature is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esignature not found")
    # A client may read their OWN signature record and no other. This is the
    # narrowest of the client scopes and doesn't need the membership tables:
    # `signed_by_user_id` (migration 0019) says exactly who signed, so
    # "mine" is a direct comparison.
    #
    # Rows signed before 0019 have a NULL here and are therefore invisible
    # to every client — deliberate. Those are precisely the records whose
    # attribution was never verified, and guessing an owner for them would
    # manufacture the evidence link this change exists to establish.
    if current.role == "client" and esignature.signed_by_user_id != current.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esignature not found")
    return esignature


@router.get("/{esignature_id}", response_model=EsignatureResponse)
async def get_esignature(
    esignature_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> EsignatureResponse:
    """`client` sees only signatures they themselves produced; staff roles
    keep blanket company-scoped (RLS-backed) read.

    This reverses resolved judgment call #2, which granted `client` blanket
    tenant-wide read here. The reasoning then was schema-level and honest:
    `Esignature` had no signer-to-user linkage column at all — only the
    free-text `signer_name`/`signer_email` captured at signing time — so
    "this client's own signatures" was not expressible. The consequence was
    that every client of a company could read every other client's executed
    contracts.

    Migration 0019 adds `signed_by_user_id`, which makes it expressible,
    so the filter is now applied in `_get_esignature_or_404`. See
    `app/services/client_scope.py` for the same reversal applied to
    estimates, change orders and invoices.
    """
    esignature = await _get_esignature_or_404(current, esignature_id)
    return EsignatureResponse.model_validate(esignature)
