import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin, UpdatedAtMixin


class CompanyBranding(Base, UUIDPKMixin, UpdatedAtMixin):
    __tablename__ = "company_branding"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False, unique=True
    )
    logo_storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    accent_color: Mapped[str] = mapped_column(String(7), nullable=False, default="#1e293b")
    footer_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # The display name outbound email goes out under (migration 0027).
    # Empty means "use the company's own name" — see
    # app/services/email_sender.py, which is the one place that decides.
    email_sender_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
