import uuid
from datetime import datetime

from sqlalchemy import Boolean, Computed, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Company(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "companies"

    # `eager_defaults=False` is load-bearing, and specifically because of
    # `is_active` below. SQLAlchemy's default ("auto") reads server-computed
    # columns back with `INSERT ... RETURNING`, and `POST /auth/register`
    # inserts the root company BEFORE any tenant context exists (design
    # decision #2, app/routers/auth.py) — so the RETURNING clause is read
    # under the `tenant_select` policy with `app.current_tenant` unset, and
    # registration fails with "new row violates row-level security policy".
    # That is not a hypothetical: it broke 28 tests when this column was
    # first made generated.
    #
    # Nothing here needs a value read back at insert time. `created_at` is
    # passed explicitly by the ORM, and `is_active` is deferred below.
    __mapper_args__ = {"eager_defaults": False}

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # DERIVED, never written (migration 0025). Postgres computes this from
    # `deleted_at` below, so the two cannot disagree — 0024 kept them in
    # step by convention, which holds until someone adds a third writer.
    #
    # `Computed` is not decoration: a generated column REJECTS an explicit
    # value, so SQLAlchemy has to be told to leave it out of INSERTs. Assign
    # to `deleted_at` to change it.
    #
    # NOT `deferred=True`, which was the obvious first answer and is wrong
    # under async: a deferred attribute loads lazily on access, and
    # `CompanyResponse.model_validate(company)` reaches for it from
    # synchronous Pydantic code, which asyncpg cannot serve — the failure is
    # `MissingGreenlet`, not a slow query. Ordinary SELECTs therefore carry
    # it, and the one route that INSERTs a company and immediately
    # serialises it refreshes the value explicitly (companies.py).
    is_active: Mapped[bool] = mapped_column(
        Boolean, Computed("deleted_at IS NULL", persisted=True)
    )
    # Soft delete, set by the platform console (migration 0024). NULL means
    # live. Enforced in app/core/deps.py at the membership chokepoint, so a
    # token already issued stops working too, not just new logins.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
