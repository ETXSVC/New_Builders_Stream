import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class PlatformAdmin(Base):
    """A user permitted to administer every tenant (migration 0023).

    Deliberately NOT a sixth value in `company_users.role`: that column
    describes a user's standing *within one company* and is CHECK-constrained
    to the five in-tenant roles. Platform administration is a global fact
    about a user, so it lives in a global table alongside `users` itself.

    No `UUIDPKMixin`: `user_id` IS the primary key. A user either holds this
    privilege or does not, and a surrogate id would permit two rows for the
    same person that disagree about `revoked_at`.

    Read access from the request path is limited by RLS to the row naming
    the current user, and INSERT/UPDATE/DELETE are revoked from both
    `app_user` and `scanner` in that migration — so no code reachable from
    an HTTP request or a background job can grant this privilege. The only
    way in is `backend/scripts/grant_platform_admin.py`, run by an operator
    as the owner role.
    """

    __tablename__ = "platform_admins"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    # Soft revocation: `is_active` below is the only thing that should ever
    # be consulted for an authorization decision.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
