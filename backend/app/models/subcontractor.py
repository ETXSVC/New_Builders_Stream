import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Subcontractor(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "subcontractors"

    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
    # clause) — same convention as Project.company_id / Estimate.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    trade: Mapped[str | None] = mapped_column(String(100), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # TimestampMixin only (no UpdatedAtMixin): the schema doc's own DDL for
    # this table gives it no `updated_at` column — same precedent as Phase
    # (app/models/phase.py) of not adding a timestamp mixin/column the
    # schema doc doesn't list.
