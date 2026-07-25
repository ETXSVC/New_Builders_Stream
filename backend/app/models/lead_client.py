import uuid

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class LeadClient(Base, UUIDPKMixin, TimestampMixin):
    """`ProjectClient`'s pre-project counterpart (migration 0019).

    An Estimate may hang off a bare Lead — `Estimate.project_id` is
    nullable — and those are precisely the estimates a prospective customer
    is asked to review and sign, before any Project exists. Without this
    table, scoping the client role by project membership alone would leave
    every new-business estimate unreachable by the person meant to sign it.
    """

    __tablename__ = "lead_clients"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (UniqueConstraint("lead_id", "user_id", name="uq_lead_clients_parent_user"),)
