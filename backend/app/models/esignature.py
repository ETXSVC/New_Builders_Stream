import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin

VALID_DOCUMENT_TYPES = ("estimate", "change_order")

_DOCUMENT_TYPE_CHECK_SQL = (
    "document_type IN (" + ",".join(f"'{doc_type}'" for doc_type in VALID_DOCUMENT_TYPES) + ")"
)


class _InetAsString(TypeDecorator):
    """Wraps postgresql.INET so the ORM-mapped value is always a plain
    `str`, matching this column's `Mapped[str]` annotation.

    Verified empirically (Task 2.17 review): asyncpg's default codec for
    `inet` columns returns `ipaddress.IPv4Address`/`IPv6Address` objects on
    read, not `str` — reproduced through the full SQLAlchemy async engine +
    asyncpg stack, not just raw asyncpg. Left unwrapped, `Mapped[str]` would
    misdescribe the actual runtime type: `.upper()` and other str-only
    methods would raise `AttributeError`, and a future Pydantic
    `EsignatureResponse.ip_address: str` (Task 2.18) would fail strict
    validation against an `IPv4Address` instance. Only the read path needs
    correcting — the write path already accepts a plain `str` unchanged.
    This only affects the Python-side mapped type; the underlying database
    column stays genuine Postgres `INET` (`impl = INET`), matching
    docs/04-database-schema.md Section 6's `ip_address INET NOT NULL`
    exactly, and the migration's own hardcoded `INET` column type is
    unaffected (0006 doesn't derive its DDL from this ORM metadata)."""

    impl = INET
    cache_ok = True

    def process_result_value(self, value, dialect):
        return str(value) if value is not None else value


class Esignature(Base, UUIDPKMixin):
    __tablename__ = "esignatures"

    # No TimestampMixin/UpdatedAtMixin: docs/04-database-schema.md Section 6's
    # `esignatures` table has no created_at/updated_at columns at all —
    # `signed_at` (below) is the meaningful timestamp, not a generic
    # created_at, and there is nothing to ever update (this row is immutable
    # from the moment it's written — see the REVOKE UPDATE, DELETE hardening
    # in migration 0006, the same treatment Phase 1 gave communication_logs/
    # daily_logs/documents, design decision #6).

    # No ondelete here, matching docs/04-database-schema.md Section 6's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
    # clause) — same convention as Project.company_id / Estimate.company_id /
    # CostCatalogItem.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    signer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    signer_email: Mapped[str] = mapped_column(String(255), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Postgres-native INET, not a String column — the schema doc specifies
    # `ip_address INET NOT NULL` explicitly (Section 6), and this is the
    # first column in the codebase to need the dedicated network-address
    # type rather than a VARCHAR. Uses _InetAsString (above), not INET
    # directly, so the ORM-mapped value is a real `str` on read, matching
    # this attribute's `Mapped[str]` annotation.
    ip_address: Mapped[str] = mapped_column(_InetAsString, nullable=False)
    # Rendered signature image/hash, retained per the Security & Compliance
    # doc (schema doc's inline comment) — a STORAGE_ROOT-relative path, same
    # convention as Estimate.pdf_storage_path / Document.storage_path.
    signature_artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # CHECK constraint mirrors the migration's DB-level constraint, same
    # belt-and-suspenders pattern as Project.ck_projects_status /
    # Estimate.ck_estimates_status.
    __table_args__ = (
        CheckConstraint(_DOCUMENT_TYPE_CHECK_SQL, name="ck_esignatures_document_type"),
    )
