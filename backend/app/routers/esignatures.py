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
    return esignature


@router.get("/{esignature_id}", response_model=EsignatureResponse)
async def get_esignature(
    esignature_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> EsignatureResponse:
    """Resolved judgment call #2: `client` gets BLANKET company-scoped read
    access here (RLS-backed), NOT a per-row filter scoped to "signatures
    this specific client signed." `Esignature` has no signer-to-user
    linkage column at all — only `signer_name`/`signer_email` (captured,
    free-text strings at signing time, not necessarily matching the exact
    case/format of a `users` row) — so there is no schema-level way to
    filter "this client's own" signatures from "any signature in this
    tenant." This mirrors design decision #3's identical resolution for
    `estimates`: `GET /estimates/{id}` (`app/routers/estimates.py`,
    `_get_estimate_or_404`) applies no signer-linkage filter for `client`
    either — blanket, tenant-scoped access, not a per-row restriction, is
    this codebase's established answer to this exact ambiguity wherever it
    has come up before.
    """
    esignature = await _get_esignature_or_404(current, esignature_id)
    return EsignatureResponse.model_validate(esignature)
