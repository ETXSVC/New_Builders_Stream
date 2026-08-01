"""Migration 0030: what a local name resolved to in a provider's books.

A QuickBooks invoice needs a `CustomerRef` naming a real Customer in the
tenant's own accounting file, and a bill needs a `VendorRef`. Builders
Stream has neither — `invoices` carries no client column, only `project_id`
— so the sync path resolves a display name to a provider id (finding it, or
creating it) and records the answer here.

Remembering is the point. Without it every sync re-searches, and a
find-or-create that forgets is a find-or-create that eventually creates a
second "Acme Holdings" in somebody's real accounting file.

One table with an `entity_kind` discriminator rather than three: the rows
and the lookup are identical in every case, so three would be three copies
of the same policy, index and upsert. See the migration for why `local_key`
is a name rather than a foreign key.
"""
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class IntegrationEntityMapping(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "integration_entity_mappings"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "entity_kind",
            "local_key",
            name="uq_integration_entity_mappings_connection_kind_key",
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # CASCADE at the database level (migration 0030): these ids mean nothing
    # once their connection is gone, and a reconnect to a different company
    # file must not inherit the previous file's ids.
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 'customer' | 'vendor' | 'account'
    entity_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    local_key: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_entity_id: Mapped[str] = mapped_column(String(100), nullable=False)
