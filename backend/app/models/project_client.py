import uuid

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class ProjectClient(Base, UUIDPKMixin, TimestampMixin):
    """Grants one `client`-role user read access to one Project, and the
    right to act on the documents hanging off it (migration 0019).

    Before this table the `client` role was a tenant-wide reader — RLS is
    company-scoped, and the client-facing routes narrowed by document
    *status* only, never by identity. This is the edge that makes
    "this client's own work" expressible at all; see
    `app/services/client_scope.py` for the single place it is consumed.

    No `updated_at`: a membership is granted or revoked, never edited.
    """

    __tablename__ = "project_clients"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # CASCADE mirrors the migration: a membership row outliving its project
    # or its user would be a dangling access grant.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_clients_parent_user"),
    )
