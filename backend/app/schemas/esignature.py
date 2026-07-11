import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr


class EsignatureCaptureRequest(BaseModel):
    """Not currently bound to any route body — Task 2.18 adds no
    `POST /esignatures` route (`docs/05-api-specification.md` Section 5 only
    lists `GET /esignatures/{id}`; capture happens by `capture_esignature`
    (`app/services/esignature.py`) being called directly from the future
    Estimate approval (Task 2.19) and Change Order approval (Task 2.22)
    endpoints). Defined here now anyway, alongside `EsignatureResponse`,
    since those future routes will need a well-formed request shape for the
    signer-identity fields, and defining it now keeps this schema next to
    the model/response shape it describes.

    Deliberately excludes `signed_at`/`ip_address`: docs/07-security-compliance.md
    Section 6's ESIGN Act intent-to-sign requirements call for capturing the
    ACTUAL originating timestamp and IP address of the signing request — a
    client-submitted value for either would defeat the entire evidentiary
    purpose of recording them (a signer could claim any timestamp/IP they
    liked). Both are captured server-side only, by the router calling
    `capture_esignature` (`Request.client.host` for the IP, `datetime.now(UTC)`
    for the timestamp — see that function's own docstring), never accepted
    from the request body.
    """

    signer_name: str
    signer_email: EmailStr


class EsignatureResponse(BaseModel):
    """Full model, `from_attributes=True` pattern matching
    `EstimateResponse`/`MarkupProfileResponse`'s own convention. `ip_address`
    is `str` here, not `ipaddress.IPv4Address`/`IPv6Address` — matching
    `Esignature.ip_address`'s own `Mapped[str]` annotation, which the
    `_InetAsString` TypeDecorator (`app/models/esignature.py`) guarantees is
    always a plain `str` on read, never an `ipaddress` object."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    signer_name: str
    signer_email: str
    signed_at: datetime
    ip_address: str
    signature_artifact_path: str
    document_type: str
