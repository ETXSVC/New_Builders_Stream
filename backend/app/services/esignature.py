"""Task 2.18: shared e-signature capture service.

This is the ONE function both Estimate approval (`POST /estimates/{id}/approve`,
`app/routers/estimates.py`, live as of Task 2.19) and Change Order approval
(Task 2.22, not yet built) call/will call to record an e-signature — design
decision #3/#6's "reuses the e-signature capability" made concrete as
actual shared code, not two independent implementations that happen to
look similar.

Per docs/07-security-compliance.md Section 6, to satisfy the intent-to-sign
standard under the U.S. ESIGN Act (and equivalent state UETA statutes), an
`esignatures` row must capture: the signer's typed/drawn signature (the
`signature_artifact_bytes` this function writes to disk), full name, email,
timestamp, and originating IP address. `signed_at`/`ip_address` are always
the REAL, server-observed values — see this function's own parameter
docstring below for why neither is ever accepted from client input.

Division of responsibility mirrors `app/services/catalog_resolution.py` /
`app/services/estimate_calculation.py`: this module does its own writing
(artifact file + `Esignature` row) and returns the fully-populated object,
but never commits (Inherited Invariant #3 — the caller, i.e. the Estimate/
Change Order approval endpoint, owns the transaction) and never writes an `audit_log`
entry itself. docs/07-security-compliance.md Section 5 enumerates
"Estimate approval, Change Order approval" as the audit-worthy actions, not
e-signature capture in isolation — that matches this codebase's established
"the caller that knows the business context writes the audit entry, not a
low-level shared helper" convention (`write_document_file` doesn't
audit-log either; `upload_document`, the router calling it, does).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Esignature
from app.models.esignature import VALID_DOCUMENT_TYPES
from app.services.document_storage import write_esignature_artifact_file


async def capture_esignature(
    session: AsyncSession,
    *,
    company_id: uuid.UUID,
    signer_name: str,
    signer_email: str,
    ip_address: str,
    document_type: str,
    signature_artifact_bytes: bytes,
) -> Esignature:
    """Writes `signature_artifact_bytes` to
    `{STORAGE_ROOT}/{company_id}/esignatures/{new_esignature_id}.png`
    (`write_esignature_artifact_file`, `app/services/document_storage.py` —
    see that function's own docstring for why `.png` is a deliberate fixed
    choice and why it exclusive-creates rather than overwrites), inserts the
    new `Esignature` row, and returns it. Does NOT commit — the caller (the
    Estimate/Change Order approval endpoint) owns the transaction, same
    Inherited Invariant #3 as every other service function in this
    codebase.

    `ip_address`: the caller is expected to pass the REAL, server-observed
    originating IP of the signing request (e.g. FastAPI's
    `Request.client.host`), never a client-submitted value — trusting a
    client-supplied IP/timestamp would defeat the entire evidentiary purpose
    ESIGN Act capture exists for (Security & Compliance Section 6). This
    function itself does no capturing of the request — that's the calling
    router's job (needs a `Request` parameter) — it only persists whatever
    `ip_address` it's handed.

    Production-correctness note (not fixed here, deliberately out of this
    task's scope): `Request.client.host` reflects the IMMEDIATE peer, which
    is a reverse proxy's own address, not the true origin client, unless
    `X-Forwarded-For` (or a similar proxy header) is parsed and trusted.
    This codebase's deployment target sits behind Traefik/Nginx (see
    docs/03-technical-architecture.md), so `X-Forwarded-For`-aware IP
    extraction is a real production-correctness gap, deferred to whenever
    that proxy layer is actually configured — flagged here rather than
    silently shipping an IP capture that's wrong for the actual deployment
    topology.

    `signed_at` is always `datetime.now(timezone.utc)`, captured at the
    moment this function runs — never accepted as a parameter, for the same
    "never trust a client-submitted intent-to-sign timestamp" reason
    `ip_address` is never accepted from client input either.

    `document_type` is checked against `VALID_DOCUMENT_TYPES`
    (`app/models/esignature.py`) in Python, BEFORE the artifact file is ever
    written — found during this task's own review: the DB's
    `ck_esignatures_document_type` CHECK constraint is still the ultimate
    authority (this check exists purely to fail fast, not as a second
    independent source of truth that could drift from the DB constraint),
    but relying on it ALONE meant a rejected insert still left behind a
    real, orphaned signature-artifact file on disk with no corresponding
    row and no way to ever discover or garbage-collect it — a filesystem
    write has no transactional relationship to the DB write that follows
    it, so letting the DB be the only check let a bad `document_type`
    value burn a file write for nothing. `document_type` will always be a
    hardcoded literal from a trusted caller (Task 2.19/2.22's approval
    endpoints), never raw user input, so this is a defensive guard against
    a caller bug, not user-input validation — hence a plain `ValueError`,
    not an `HTTPException` (this is a low-level shared service function,
    not a router).
    """
    if document_type not in VALID_DOCUMENT_TYPES:
        raise ValueError(f"document_type must be one of {VALID_DOCUMENT_TYPES}, got {document_type!r}")

    esignature_id = uuid.uuid4()

    signature_artifact_path = write_esignature_artifact_file(
        company_id=company_id,
        esignature_id=esignature_id,
        content=signature_artifact_bytes,
    )

    esignature = Esignature(
        id=esignature_id,
        company_id=company_id,
        signer_name=signer_name,
        signer_email=signer_email,
        signed_at=datetime.now(timezone.utc),
        ip_address=ip_address,
        signature_artifact_path=signature_artifact_path,
        document_type=document_type,
    )
    session.add(esignature)
    await session.flush()

    return esignature
