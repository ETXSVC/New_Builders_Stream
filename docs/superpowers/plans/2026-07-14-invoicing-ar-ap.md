# Invoicing / AR-AP / Profitability Reporting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build client-facing Invoices (AR), vendor Bills (AP), Expenses, and a company-wide profitability report with AR/AP aging and estimated tax liability, per `docs/superpowers/specs/2026-07-14-invoicing-ar-ap-design.md`.

**Architecture:** Five new tables (`invoices`, `invoice_payments`, `bills`, `bill_payments`, `expenses`) behind standard `tenant_isolation` RLS policies. An `ESTIMATE_APPROVED` event consumer auto-creates a draft deposit Invoice. A unified daily scheduled job flags overdue Invoices/Bills. `block_if_read_only` (already built by the Billing plan) is wired into every new write route from the start.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, PostgreSQL RLS, Dramatiq + APScheduler (existing infra, no new services).

**Numbering note:** continues Phase 3's task numbering after Billing's own plan (3.15–3.30) — this plan's tasks are 3.31–3.50.

---

### Task 3.31: Invoice, InvoicePayment, Bill, BillPayment, Expense Models

**Files:**
- Create: `backend/app/models/invoice.py`
- Create: `backend/app/models/invoice_payment.py`
- Create: `backend/app/models/bill.py`
- Create: `backend/app/models/bill_payment.py`
- Create: `backend/app/models/expense.py`
- Modify: `backend/app/models/__init__.py`

- [ ] **Step 1: Write `backend/app/models/invoice.py`**

```python
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin

VALID_INVOICE_STATUSES = ("draft", "sent", "paid", "overdue", "void")

_STATUS_CHECK_SQL = (
    "status IN (" + ",".join(f"'{status}'" for status in VALID_INVOICE_STATUSES) + ")"
)


class Invoice(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "invoices"

    # No ondelete, matching docs/04-database-schema.md Section 7's
    # `project_id UUID NOT NULL REFERENCES projects(id)` (no ON DELETE
    # clause) — same convention as Estimate.project_id (when not NULL).
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # Nullable: NULL for invoices created directly via POST /projects/{id}/invoices,
    # not auto-generated from an approved Estimate.
    estimate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("estimates.id"), nullable=True
    )
    # NOT globally unique — unique PER COMPANY (see UniqueConstraint below).
    # Two different companies both generating "INV-2026-0001" as their own
    # first invoice is expected, not a collision.
    invoice_number: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    due_date: Mapped[date | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(_STATUS_CHECK_SQL, name="ck_invoices_status"),
        UniqueConstraint("company_id", "invoice_number", name="uq_invoices_company_invoice_number"),
    )
```

- [ ] **Step 2: Write `backend/app/models/invoice_payment.py`**

```python
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class InvoicePayment(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "invoice_payments"

    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    paid_date: Mapped[date] = mapped_column(nullable=False)
    recorded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
```

- [ ] **Step 3: Write `backend/app/models/bill.py`**

```python
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin

VALID_BILL_STATUSES = ("unpaid", "paid", "overdue", "void")

_STATUS_CHECK_SQL = (
    "status IN (" + ",".join(f"'{status}'" for status in VALID_BILL_STATUSES) + ")"
)
_VENDOR_CHECK_SQL = "subcontractor_id IS NOT NULL OR vendor_name IS NOT NULL"


class Bill(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "bills"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # Nullable: NULL for company-wide overhead bills with no Project.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True
    )
    # Nullable: NULL for a vendor that isn't a tracked Subcontractor.
    subcontractor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subcontractors.id"), nullable=True
    )
    # Required only when subcontractor_id is NULL — enforced by the
    # ck_bills_vendor CheckConstraint below, not at the Python/Pydantic layer
    # alone (defense in depth, matching this codebase's general convention
    # of mirroring app-layer validation with a DB constraint where possible).
    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The vendor's own reference number, free text — never generated by us
    # (unlike Invoice.invoice_number).
    bill_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unpaid")
    due_date: Mapped[date | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(_STATUS_CHECK_SQL, name="ck_bills_status"),
        CheckConstraint(_VENDOR_CHECK_SQL, name="ck_bills_vendor"),
    )
```

- [ ] **Step 4: Write `backend/app/models/bill_payment.py`**

```python
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class BillPayment(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "bill_payments"

    bill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bills.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    paid_date: Mapped[date] = mapped_column(nullable=False)
    recorded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
```

- [ ] **Step 5: Write `backend/app/models/expense.py`**

```python
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin


class Expense(Base, UUIDPKMixin):
    __tablename__ = "expenses"

    # No TimestampMixin: docs/04-database-schema.md Section 7's expenses
    # table has no created_at column — incurred_on is the meaningful date
    # for this row, same "the domain date plays the timestamp role" pattern
    # ComplianceNotification.fired_at already establishes.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    incurred_on: Mapped[date] = mapped_column(nullable=False)
```

- [ ] **Step 6: Register the new models in `backend/app/models/__init__.py`**

Add these imports after `from app.models.subscription import Subscription`:

```python
from app.models.invoice import Invoice
from app.models.invoice_payment import InvoicePayment
from app.models.bill import Bill
from app.models.bill_payment import BillPayment
from app.models.expense import Expense
```

Add `"Invoice"`, `"InvoicePayment"`, `"Bill"`, `"BillPayment"`, `"Expense"` to the `__all__` list, after `"Subscription"`.

- [ ] **Step 7: Verify the models import cleanly**

Run: `cd backend && source .venv/Scripts/activate && python -c "from app.models import Invoice, InvoicePayment, Bill, BillPayment, Expense; print('ok')"`
Expected: `ok`

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/invoice.py backend/app/models/invoice_payment.py backend/app/models/bill.py backend/app/models/bill_payment.py backend/app/models/expense.py backend/app/models/__init__.py
git commit -m "feat: add Invoice, InvoicePayment, Bill, BillPayment, Expense models"
```

---

### Task 3.32: Migration (0012) — AR/AP/Expense Schema + RLS

**Files:**
- Create: `backend/migrations/versions/0012_invoicing_ar_ap_schema.py`

- [ ] **Step 1: Write the migration**

Column/type/nullability/constraint-name choices must mirror Task 3.31's ORM models exactly, same "migration and model both declare the same DB-level constraint" belt-and-suspenders pattern every prior migration uses. All five tables get the standard `tenant_isolation` policy shape (downward-visibility via `get_all_descendant_ids`, the same shape `0009`/`0010` use for plain company-scoped tables — none of these five are root-only like `subscriptions`).

```python
"""Invoicing AR/AP schema: invoices, invoice_payments, bills, bill_payments,
expenses, and their RLS policies.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-14

Per docs/superpowers/specs/2026-07-14-invoicing-ar-ap-design.md Section 1.
Five tables, created in FK-dependency order:

  invoices          -> projects, companies, estimates
  invoice_payments  -> invoices, companies, users
  bills             -> companies, projects, subcontractors
  bill_payments     -> bills, companies, users
  expenses          -> projects, companies

All five are plain, flat, company-scoped resources (no hierarchy/bidirectional
concern of their own) — each gets the standard single, non-inherited
`tenant_isolation` policy, the same shape 0008/0009 give change_orders/
subcontractors: a single FOR ALL policy with matching USING/WITH CHECK, using
the guarded NULLIF(current_setting('app.current_tenant', true), '')::uuid
cast routed through get_all_descendant_ids() so a parent company's session
also sees its descendant branches' rows. None of these are root-only like
`subscriptions` (migration 0010) — no upward-visibility policy needed here.

No REVOKE on any of the five: every table has a real, planned mutation path
(payments append, void transitions status, and nothing here is
"immutable by omission" the way esignatures/compliance_documents are) —
defaults to the ordinary app_user grants from 0001's ALTER DEFAULT
PRIVILEGES, same as subcontractor_assignments (0009).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- invoices --------------------------------------------------------
    op.create_table(
        "invoices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column(
            "estimate_id", UUID(as_uuid=True), sa.ForeignKey("estimates.id"), nullable=True
        ),
        sa.Column("invoice_number", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('draft','sent','paid','overdue','void')", name="ck_invoices_status"
        ),
        # NOT globally unique — unique PER COMPANY. Numbering
        # (app/services/invoicing.py's next_invoice_number, Task 3.33) is a
        # per-company sequential counter, so two different companies both
        # generating "INV-2026-0001" as their own first invoice is expected,
        # not a collision. A plain `unique=True` on the column alone was
        # caught in this task's own code-quality review as a real
        # multi-tenant bug — fixed here before this migration ships.
        sa.UniqueConstraint("company_id", "invoice_number", name="uq_invoices_company_invoice_number"),
    )
    op.create_index("idx_invoices_company_status", "invoices", ["company_id", "status"])

    op.execute("ALTER TABLE invoices ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON invoices FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- invoice_payments --------------------------------------------------
    op.create_table(
        "invoice_payments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "invoice_id",
            UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("paid_date", sa.Date, nullable=False),
        sa.Column(
            "recorded_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute("ALTER TABLE invoice_payments ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON invoice_payments FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- bills -------------------------------------------------------------
    op.create_table(
        "bills",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column(
            "project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True
        ),
        sa.Column(
            "subcontractor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subcontractors.id"),
            nullable=True,
        ),
        sa.Column("vendor_name", sa.String(255), nullable=True),
        sa.Column("bill_number", sa.String(50), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="unpaid"),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('unpaid','paid','overdue','void')", name="ck_bills_status"
        ),
        sa.CheckConstraint(
            "subcontractor_id IS NOT NULL OR vendor_name IS NOT NULL", name="ck_bills_vendor"
        ),
    )
    op.create_index("idx_bills_company_status", "bills", ["company_id", "status"])

    op.execute("ALTER TABLE bills ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON bills FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- bill_payments -------------------------------------------------------
    op.create_table(
        "bill_payments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bill_id",
            UUID(as_uuid=True),
            sa.ForeignKey("bills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("paid_date", sa.Date, nullable=False),
        sa.Column(
            "recorded_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute("ALTER TABLE bill_payments ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON bill_payments FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- expenses ------------------------------------------------------------
    op.create_table(
        "expenses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("incurred_on", sa.Date, nullable=False),
    )

    op.execute("ALTER TABLE expenses ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON expenses FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON expenses")
    op.drop_table("expenses")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON bill_payments")
    op.drop_table("bill_payments")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON bills")
    op.drop_table("bills")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON invoice_payments")
    op.drop_table("invoice_payments")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON invoices")
    op.drop_table("invoices")
```

- [ ] **Step 2: Run the migration against the local test database**

Run: `cd backend && source .venv/Scripts/activate && alembic upgrade head`
Expected: migration `0012` applies with no errors.

- [ ] **Step 3: Verify RLS is enabled and policies exist**

Run:
```bash
docker exec <postgres-container> psql -U postgres -d builders_stream_test -c "\d+ invoices" 
docker exec <postgres-container> psql -U postgres -d builders_stream_test -c "SELECT tablename, policyname FROM pg_policies WHERE tablename IN ('invoices','invoice_payments','bills','bill_payments','expenses');"
```
Expected: all five tables show `Row security` enabled and a `tenant_isolation` policy each.

- [ ] **Step 4: Commit**

```bash
git add backend/migrations/versions/0012_invoicing_ar_ap_schema.py
git commit -m "feat: add AR/AP/Expense schema migration (invoices, payments, bills, expenses)"
```

---

### Task 3.33: Invoicing Service — Constants + Invoice-Numbering Helper

**Files:**
- Create: `backend/app/services/invoicing.py`
- Test: `backend/tests/test_invoicing_service.py`

- [ ] **Step 1: Write the failing test**

```python
import uuid
from decimal import Decimal

from app.models import Company, Invoice
from app.services.invoicing import DEFAULT_DEPOSIT_PERCENTAGE, DEFAULT_TAX_RATE, next_invoice_number


async def test_default_constants_are_reasonable_placeholders():
    assert Decimal("0") < DEFAULT_DEPOSIT_PERCENTAGE <= Decimal("1")
    assert Decimal("0") <= DEFAULT_TAX_RATE < Decimal("1")


async def test_next_invoice_number_starts_at_one_for_a_new_company(db_session):
    company = Company(id=uuid.uuid4(), parent_id=None, name="Numbering Co")
    db_session.add(company)
    await db_session.flush()

    number = await next_invoice_number(db_session, company.id)
    assert number.startswith("INV-")
    assert number.endswith("-0001")


async def test_next_invoice_number_increments_per_company(db_session):
    company = Company(id=uuid.uuid4(), parent_id=None, name="Numbering Co 2")
    db_session.add(company)
    await db_session.flush()

    first = await next_invoice_number(db_session, company.id)
    invoice = Invoice(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        company_id=company.id,
        invoice_number=first,
        amount=Decimal("100.00"),
        status="draft",
    )
    db_session.add(invoice)
    await db_session.flush()

    second = await next_invoice_number(db_session, company.id)
    assert second.endswith("-0002")


async def test_next_invoice_number_is_independent_per_company(db_session):
    company_a = Company(id=uuid.uuid4(), parent_id=None, name="Co A")
    company_b = Company(id=uuid.uuid4(), parent_id=None, name="Co B")
    db_session.add_all([company_a, company_b])
    await db_session.flush()

    number_a = await next_invoice_number(db_session, company_a.id)
    invoice_a = Invoice(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        company_id=company_a.id,
        invoice_number=number_a,
        amount=Decimal("50.00"),
        status="draft",
    )
    db_session.add(invoice_a)
    await db_session.flush()

    number_b = await next_invoice_number(db_session, company_b.id)
    assert number_b.endswith("-0001"), "company B's own numbering must not be affected by company A's invoices"
```

This test file needs a `db_session` fixture giving a real owner-role `AsyncSession` with RLS bypassed (invoice numbering must work across companies in one test, which a tenant-scoped app_user session can't do). Check `tests/conftest.py` for an existing owner-role session fixture before adding a new one — if none exists, add a module-local one following the same `create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)` pattern `test_read_only_enforcement.py`'s `_make_current_user_for_status` uses, remembering to `await session.rollback()` then `await session.close()` before `await engine.dispose()` in a `finally`/fixture-teardown block (see `feedback_docker_test_hangs` precedent — a bare session cleanup that skips rollback+close leaks an "idle in transaction" connection that can hang a later test's TRUNCATE-based cleanup for hours).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoicing_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.invoicing'`

- [ ] **Step 3: Write `backend/app/services/invoicing.py`**

```python
"""Task 3.33 (design spec Section 1). Deposit/tax-rate placeholders and the
per-company invoice-numbering helper, mirroring app/services/billing.py's
own module-level-constants pattern.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice

# Explicit placeholders, not a validated business decision — same status as
# app/services/billing.py's TIER_INCLUDED_SEATS. 10% deposit, 0% tax
# (a company with no configured tax obligation shows $0 estimated liability
# rather than an invented nonzero default).
DEFAULT_DEPOSIT_PERCENTAGE = Decimal("0.10")
DEFAULT_TAX_RATE = Decimal("0.00")


async def next_invoice_number(session: AsyncSession, company_id: uuid.UUID) -> str:
    """Per-company sequential, formatted INV-{creation_year}-{counter}. The
    year is cosmetic (when this invoice was created), not an annual reset
    boundary — the counter itself never resets (design spec Section 2).

    pg_advisory_xact_lock, keyed on hashtext(company_id), serializes
    concurrent number generation for the SAME company within the current
    transaction (auto-released at commit/rollback) — a bare `SELECT
    COUNT(*) + 1` with no lock would race under concurrent invoice creation
    for the same company (two transactions both counting before either
    inserts). This codebase has no existing per-tenant DB sequence to reuse,
    so a locked-count approach is used rather than introducing new sequence
    infrastructure for one column.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:company_id)::bigint)"),
        {"company_id": str(company_id)},
    )
    result = await session.execute(
        select(func.count()).select_from(Invoice).where(Invoice.company_id == company_id)
    )
    count = result.scalar_one()
    year = datetime.now(timezone.utc).year
    return f"INV-{year}-{count + 1:04d}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoicing_service.py -v`
Expected: PASS, 4 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/invoicing.py backend/tests/test_invoicing_service.py
git commit -m "feat: add invoicing service (deposit/tax constants, invoice numbering)"
```

---

### Task 3.34: Invoice, Bill, Expense, Profitability Report Schemas

**Files:**
- Create: `backend/app/schemas/invoice.py`
- Create: `backend/app/schemas/bill.py`
- Create: `backend/app/schemas/expense.py`
- Create: `backend/app/schemas/profitability.py`

- [ ] **Step 1: Write `backend/app/schemas/invoice.py`**

```python
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class InvoiceCreateRequest(BaseModel):
    amount: Decimal
    due_date: date | None = None


class InvoiceSendRequest(BaseModel):
    due_date: date | None = None


class InvoicePaymentCreateRequest(BaseModel):
    amount: Decimal
    paid_date: date


class InvoicePaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    paid_date: date
    recorded_by: uuid.UUID
    created_at: datetime


class InvoiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    company_id: uuid.UUID
    estimate_id: uuid.UUID | None
    invoice_number: str
    amount: Decimal
    status: str
    due_date: date | None
    created_at: datetime
    outstanding_balance: Decimal


class InvoiceDetailResponse(InvoiceResponse):
    payments: list[InvoicePaymentResponse]


class InvoiceListResponse(BaseModel):
    items: list[InvoiceResponse]
    next_cursor: str | None
```

- [ ] **Step 2: Write `backend/app/schemas/bill.py`**

```python
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator


class BillCreateRequest(BaseModel):
    project_id: uuid.UUID | None = None
    subcontractor_id: uuid.UUID | None = None
    vendor_name: str | None = None
    amount: Decimal
    due_date: date | None = None
    bill_number: str | None = None

    @model_validator(mode="after")
    def _require_a_vendor(self) -> "BillCreateRequest":
        if self.subcontractor_id is None and not self.vendor_name:
            raise ValueError("Either subcontractor_id or vendor_name is required")
        return self


class BillPaymentCreateRequest(BaseModel):
    amount: Decimal
    paid_date: date


class BillPaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bill_id: uuid.UUID
    amount: Decimal
    paid_date: date
    recorded_by: uuid.UUID
    created_at: datetime


class BillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    project_id: uuid.UUID | None
    subcontractor_id: uuid.UUID | None
    vendor_name: str | None
    bill_number: str | None
    amount: Decimal
    status: str
    due_date: date | None
    created_at: datetime
    outstanding_balance: Decimal


class BillDetailResponse(BillResponse):
    payments: list[BillPaymentResponse]


class BillListResponse(BaseModel):
    items: list[BillResponse]
    next_cursor: str | None
```

- [ ] **Step 3: Write `backend/app/schemas/expense.py`**

```python
import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ExpenseCreateRequest(BaseModel):
    description: str
    amount: Decimal
    incurred_on: date


class ExpenseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    company_id: uuid.UUID
    description: str
    amount: Decimal
    incurred_on: date


class ExpenseListResponse(BaseModel):
    items: list[ExpenseResponse]
    next_cursor: str | None
```

- [ ] **Step 4: Write `backend/app/schemas/profitability.py`**

```python
import uuid
from decimal import Decimal

from pydantic import BaseModel


class ProjectProfitability(BaseModel):
    project_id: uuid.UUID
    billed_revenue: Decimal
    actual_cost: Decimal
    profitability: Decimal


class AgingEntry(BaseModel):
    id: uuid.UUID
    outstanding_balance: Decimal
    due_date: str | None
    bucket: str


class ProfitabilityReportResponse(BaseModel):
    projects: list[ProjectProfitability]
    ar_aging: list[AgingEntry]
    ap_aging: list[AgingEntry]
    tax_liability_estimate: Decimal
```

- [ ] **Step 5: Verify schemas import cleanly**

Run: `cd backend && source .venv/Scripts/activate && python -c "from app.schemas.invoice import InvoiceResponse; from app.schemas.bill import BillResponse; from app.schemas.expense import ExpenseResponse; from app.schemas.profitability import ProfitabilityReportResponse; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/invoice.py backend/app/schemas/bill.py backend/app/schemas/expense.py backend/app/schemas/profitability.py
git commit -m "feat: add Invoice, Bill, Expense, ProfitabilityReport schemas"
```

---

### Task 3.35: POST/GET /projects/{id}/invoices, GET /invoices/{id}

**Files:**
- Create: `backend/app/routers/invoices.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_invoices.py`

- [ ] **Step 1: Write the failing tests**

```python
from decimal import Decimal

import pytest


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _create_project(client, headers, name="Test Project"):
    response = await client.post(
        "/projects",
        json={"name": name, "site_address": "123 Main St", "status": "active"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_invoice_assigns_sequential_number_and_draft_status(client):
    admin = await _register_and_login(client, "Invoice Co", "invoice-create@example.test")
    project = await _create_project(client, admin["headers"])

    response = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "1000.00"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["invoice_number"].startswith("INV-")
    assert body["invoice_number"].endswith("-0001")
    assert body["outstanding_balance"] == "1000.00"


async def test_second_invoice_for_same_company_gets_the_next_number(client):
    admin = await _register_and_login(client, "Invoice Co 2", "invoice-seq@example.test")
    project = await _create_project(client, admin["headers"])

    first = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "500.00"}, headers=admin["headers"]
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "750.00"}, headers=admin["headers"]
    )
    assert second.status_code == 201, second.text
    assert second.json()["invoice_number"].endswith("-0002")


async def test_project_manager_cannot_create_invoice(client):
    admin = await _register_and_login(client, "Invoice Co 3", "invoice-pm@example.test")
    project = await _create_project(client, admin["headers"])

    invite = await client.post(
        "/invitations",
        json={"email": "pm@example.test", "role": "project_manager"},
        headers=admin["headers"],
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    assert accept.status_code == 200, accept.text
    pm_login = await client.post(
        "/auth/login", json={"email": "pm@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=pm_headers
    )
    assert response.status_code == 403


async def test_list_invoices_for_project(client):
    admin = await _register_and_login(client, "Invoice Co 4", "invoice-list@example.test")
    project = await _create_project(client, admin["headers"])
    await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "200.00"}, headers=admin["headers"]
    )

    response = await client.get(f"/projects/{project['id']}/invoices", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


async def test_get_invoice_detail_includes_empty_payments_list(client):
    admin = await _register_and_login(client, "Invoice Co 5", "invoice-detail@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "300.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    response = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["payments"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoices.py -v`
Expected: FAIL — routes don't exist yet (404s)

- [ ] **Step 3: Write `backend/app/routers/invoices.py`**

```python
"""Task 3.35 (design spec Section 3): POST/GET /projects/{id}/invoices,
GET /invoices/{id}.

RBAC per docs/07-security-compliance.md Section 2's split "Accounting/Billing
(AR)" row: Admin/Accountant write, Admin/Accountant/Client read (Client
scoped to non-draft only — same `if current.role == "client":
query = query.where(...)` shape list_estimates already uses).
"""
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import Invoice, InvoicePayment
from app.routers.projects import _get_project_or_404
from app.schemas.invoice import (
    InvoiceCreateRequest,
    InvoiceDetailResponse,
    InvoiceListResponse,
    InvoicePaymentResponse,
    InvoiceResponse,
)
from app.services.invoicing import next_invoice_number

router = APIRouter(tags=["invoices"])

_WRITE_ROLES = ("admin", "accountant")
_READ_ROLES = ("admin", "accountant", "client")


async def _get_invoice_or_404(current: CurrentUser, invoice_id: uuid.UUID) -> Invoice:
    result = await current.session.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")
    return invoice


async def _paid_amount(current: CurrentUser, invoice_id: uuid.UUID) -> Decimal:
    result = await current.session.execute(
        select(func.coalesce(func.sum(InvoicePayment.amount), 0)).where(
            InvoicePayment.invoice_id == invoice_id
        )
    )
    return result.scalar_one()


async def _invoice_response(current: CurrentUser, invoice: Invoice) -> InvoiceResponse:
    paid = await _paid_amount(current, invoice.id)
    return InvoiceResponse(
        id=invoice.id,
        project_id=invoice.project_id,
        company_id=invoice.company_id,
        estimate_id=invoice.estimate_id,
        invoice_number=invoice.invoice_number,
        amount=invoice.amount,
        status=invoice.status,
        due_date=invoice.due_date,
        created_at=invoice.created_at,
        outstanding_balance=invoice.amount - paid,
    )


@router.post(
    "/projects/{project_id}/invoices",
    response_model=InvoiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_invoice(
    project_id: uuid.UUID,
    body: InvoiceCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> InvoiceResponse:
    project = await _get_project_or_404(current, project_id)

    invoice_number = await next_invoice_number(current.session, project.company_id)
    invoice = Invoice(
        id=uuid.uuid4(),
        project_id=project.id,
        company_id=project.company_id,
        estimate_id=None,
        invoice_number=invoice_number,
        amount=body.amount,
        status="draft",
        due_date=body.due_date,
    )
    current.session.add(invoice)
    await current.session.flush()
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns.

    return await _invoice_response(current, invoice)


@router.get("/projects/{project_id}/invoices", response_model=InvoiceListResponse)
async def list_invoices(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> InvoiceListResponse:
    project = await _get_project_or_404(current, project_id)

    query = select(Invoice).where(Invoice.project_id == project.id)
    if current.role == "client":
        query = query.where(Invoice.status != "draft")

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Invoice.created_at,
        id_col=Invoice.id,
        cursor=cursor,
        limit=limit,
    )

    items = [await _invoice_response(current, row) for row in rows]
    return InvoiceListResponse(items=items, next_cursor=next_cursor)


@router.get("/invoices/{invoice_id}", response_model=InvoiceDetailResponse)
async def get_invoice(
    invoice_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> InvoiceDetailResponse:
    invoice = await _get_invoice_or_404(current, invoice_id)
    if current.role == "client" and invoice.status == "draft":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Invoice not found")

    payments_result = await current.session.execute(
        select(InvoicePayment).where(InvoicePayment.invoice_id == invoice.id)
    )
    payments = [
        InvoicePaymentResponse.model_validate(payment) for payment in payments_result.scalars().all()
    ]

    base = await _invoice_response(current, invoice)
    return InvoiceDetailResponse(**base.model_dump(), payments=payments)
```

- [ ] **Step 4: Register the router in `backend/app/main.py`**

Add `from app.routers import invoices` alongside the other router imports, and `app.include_router(invoices.router)` alongside the other `include_router` calls (after `subscriptions.router`, before `webhooks.router` — order doesn't matter functionally, just keep it near the other Phase 3 routers for readability).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoices.py -v`
Expected: PASS, 5 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/invoices.py backend/app/main.py backend/tests/test_invoices.py
git commit -m "feat: add POST/GET /projects/{id}/invoices, GET /invoices/{id}"
```

---

### Task 3.36: POST /invoices/{id}/send

**Files:**
- Modify: `backend/app/routers/invoices.py`
- Modify: `backend/tests/test_invoices.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_invoices.py`:

```python
async def test_send_invoice_with_no_due_date_at_creation_requires_one_in_the_request(client):
    admin = await _register_and_login(client, "Send Co 1", "send-1@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "400.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    missing_due_date = await client.post(
        f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"]
    )
    assert missing_due_date.status_code == 422

    response = await client.post(
        f"/invoices/{invoice_id}/send", json={"due_date": "2026-08-15"}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "sent"
    assert body["due_date"] == "2026-08-15"


async def test_send_invoice_that_already_has_a_due_date_does_not_require_one_again(client):
    admin = await _register_and_login(client, "Send Co 2", "send-2@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "400.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create.json()["id"]

    response = await client.post(
        f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["due_date"] == "2026-09-01"


async def test_sending_a_non_draft_invoice_returns_409(client):
    admin = await _register_and_login(client, "Send Co 3", "send-3@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "400.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    response = await client.post(
        f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"]
    )
    assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoices.py -v -k send`
Expected: FAIL — route doesn't exist yet (404s)

- [ ] **Step 3: Add the route to `backend/app/routers/invoices.py`**

Add this import at the top alongside the existing schema imports: `InvoiceSendRequest` (from `app.schemas.invoice`).

Add this route after `create_invoice`:

```python
@router.post("/invoices/{invoice_id}/send", response_model=InvoiceResponse)
async def send_invoice(
    invoice_id: uuid.UUID,
    body: InvoiceSendRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> InvoiceResponse:
    invoice = await _get_invoice_or_404(current, invoice_id)
    if invoice.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a draft invoice can be sent")

    due_date = body.due_date or invoice.due_date
    if due_date is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "due_date is required (either already set, or provided in this request)",
        )

    invoice.status = "sent"
    invoice.due_date = due_date
    await current.session.flush()

    return await _invoice_response(current, invoice)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoices.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/invoices.py backend/tests/test_invoices.py
git commit -m "feat: add POST /invoices/{id}/send"
```

---

### Task 3.37: POST /invoices/{id}/payments

**Files:**
- Modify: `backend/app/routers/invoices.py`
- Modify: `backend/tests/test_invoices.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_invoices.py`:

```python
async def _create_and_send_invoice(client, headers, project_id, amount, due_date="2026-09-01"):
    create = await client.post(
        f"/projects/{project_id}/invoices",
        json={"amount": amount, "due_date": due_date},
        headers=headers,
    )
    assert create.status_code == 201, create.text
    invoice_id = create.json()["id"]
    send = await client.post(f"/invoices/{invoice_id}/send", json={}, headers=headers)
    assert send.status_code == 200, send.text
    return invoice_id


async def test_partial_payment_leaves_invoice_sent_with_reduced_outstanding_balance(client):
    admin = await _register_and_login(client, "Pay Co 1", "pay-1@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "1000.00")

    response = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "400.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text

    detail = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "sent"
    assert body["outstanding_balance"] == "600.00"
    assert len(body["payments"]) == 1


async def test_cumulative_payment_reaching_full_amount_auto_marks_paid(client):
    admin = await _register_and_login(client, "Pay Co 2", "pay-2@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "500.00")

    await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "300.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    second = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "200.00", "paid_date": "2026-08-02"},
        headers=admin["headers"],
    )
    assert second.status_code == 201, second.text

    detail = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "paid"
    assert body["outstanding_balance"] == "0.00"


async def test_overpayment_leaves_outstanding_balance_negative(client):
    admin = await _register_and_login(client, "Pay Co 3", "pay-3@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "100.00")

    await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "150.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )

    detail = await client.get(f"/invoices/{invoice_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "paid"
    assert body["outstanding_balance"] == "-50.00"


async def test_payment_against_a_draft_invoice_returns_409(client):
    admin = await _register_and_login(client, "Pay Co 4", "pay-4@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    response = await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "50.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )
    assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoices.py -v -k payment`
Expected: FAIL — route doesn't exist yet

- [ ] **Step 3: Add the route to `backend/app/routers/invoices.py`**

Add `InvoicePaymentCreateRequest` to the schema imports.

Add this route after `send_invoice`:

```python
@router.post(
    "/invoices/{invoice_id}/payments",
    response_model=InvoicePaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_invoice_payment(
    invoice_id: uuid.UUID,
    body: InvoicePaymentCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> InvoicePaymentResponse:
    invoice = await _get_invoice_or_404(current, invoice_id)
    if invoice.status in ("draft", "void"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Cannot record a payment against a {invoice.status} invoice"
        )

    payment = InvoicePayment(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        company_id=invoice.company_id,
        amount=body.amount,
        paid_date=body.paid_date,
        recorded_by=current.user.id,
    )
    current.session.add(payment)
    await current.session.flush()

    paid = await _paid_amount(current, invoice.id)
    if paid >= invoice.amount:
        invoice.status = "paid"
        await current.session.flush()

    # docs/07-security-compliance.md Section 5 lists "Invoice send/payment/
    # void" among the state changes requiring an audit_log row — same
    # requirement Task 3.36's send_invoice already satisfies.
    await write_audit_log(
        current.session,
        company_id=invoice.company_id,
        actor_id=current.user.id,
        action="invoice.payment_recorded",
        entity_type="invoice",
        entity_id=invoice.id,
        metadata={"payment_id": str(payment.id), "amount": str(body.amount)},
    )

    return InvoicePaymentResponse.model_validate(payment)
```

Add `from app.services.audit import write_audit_log` to the imports if it isn't already there (Task 3.36 should have added it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoices.py -v`
Expected: PASS — exact count depends on how many extra RBAC/coverage tests earlier tasks' code-quality reviews added beyond the plan's own baseline; check the actual current test count in the file rather than a stale number here (plan originally said 12, but earlier tasks' reviews already added extra tests, so the real baseline is higher).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/invoices.py backend/tests/test_invoices.py
git commit -m "feat: add POST /invoices/{id}/payments with auto-paid transition"
```

---

### Task 3.38: POST /invoices/{id}/void

**Files:**
- Modify: `backend/app/routers/invoices.py`
- Modify: `backend/tests/test_invoices.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_invoices.py`:

```python
async def test_void_a_draft_invoice(client):
    admin = await _register_and_login(client, "Void Co 1", "void-1@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]

    response = await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "void"


async def test_void_a_sent_invoice(client):
    admin = await _register_and_login(client, "Void Co 2", "void-2@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "200.00")

    response = await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "void"


async def test_void_a_paid_invoice_returns_409(client):
    admin = await _register_and_login(client, "Void Co 3", "void-3@example.test")
    project = await _create_project(client, admin["headers"])
    invoice_id = await _create_and_send_invoice(client, admin["headers"], project["id"], "100.00")
    await client.post(
        f"/invoices/{invoice_id}/payments",
        json={"amount": "100.00", "paid_date": "2026-08-01"},
        headers=admin["headers"],
    )

    response = await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])
    assert response.status_code == 409


async def test_void_an_already_void_invoice_returns_409(client):
    admin = await _register_and_login(client, "Void Co 4", "void-4@example.test")
    project = await _create_project(client, admin["headers"])
    create = await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    invoice_id = create.json()["id"]
    await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])

    response = await client.post(f"/invoices/{invoice_id}/void", headers=admin["headers"])
    assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoices.py -v -k void`
Expected: FAIL — route doesn't exist yet

- [ ] **Step 3: Add the route to `backend/app/routers/invoices.py`**

```python
@router.post("/invoices/{invoice_id}/void", response_model=InvoiceResponse)
async def void_invoice(
    invoice_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> InvoiceResponse:
    invoice = await _get_invoice_or_404(current, invoice_id)
    if invoice.status in ("paid", "void"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot void a {invoice.status} invoice")

    invoice.status = "void"
    await current.session.flush()

    # docs/07-security-compliance.md Section 5 lists "Invoice send/payment/
    # void" among the state changes requiring an audit_log row.
    await write_audit_log(
        current.session,
        company_id=invoice.company_id,
        actor_id=current.user.id,
        action="invoice.voided",
        entity_type="invoice",
        entity_id=invoice.id,
    )

    return await _invoice_response(current, invoice)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoices.py -v`
Expected: PASS, roughly 17-20 passed (exact count depends on how many extra RBAC/coverage tests earlier tasks' code-quality reviews added beyond the plan's own baseline — check the actual current test count in the file rather than a stale number here)

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/invoices.py backend/tests/test_invoices.py
git commit -m "feat: add POST /invoices/{id}/void"
```

---

### Task 3.39: ESTIMATE_APPROVED → Draft Invoice Event Handler

**Files:**
- Create: `backend/app/services/estimate_approved_handler.py`
- Modify: `backend/app/core/event_handlers.py`
- Test: `backend/tests/test_estimate_approved_handler.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Task 3.39: ESTIMATE_APPROVED -> draft Invoice wiring (design spec Section
2). Mirrors tests/test_lead_won_drafts_project.py's own structure and
register_event_handlers() discipline."""
import json
import uuid
from decimal import Decimal

import asyncpg
import pytest

from app.core.event_handlers import register_event_handlers
from tests.conftest import TEST_DATABASE_URL

ADMIN_CONN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _create_project(client, headers, name="Deposit Project"):
    response = await client.post(
        "/projects", json={"name": name, "site_address": "1 Main St", "status": "active"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_markup_profile(client, headers):
    response = await client.post(
        "/markup-profiles",
        json={"name": "Standard", "overhead_pct": "10.00", "profit_pct": "15.00"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_catalog_item(client, headers):
    response = await client.post(
        "/catalogs/items",
        json={"name": "Lumber", "unit": "board_ft", "unit_rate": "5.00"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_and_approve_estimate(client, headers, project_id, markup_profile_id, catalog_item_id):
    create = await client.post(
        "/estimates",
        json={"project_id": project_id, "markup_profile_id": markup_profile_id},
        headers=headers,
    )
    assert create.status_code == 201, create.text
    estimate_id = create.json()["id"]

    lines = await client.put(
        f"/estimates/{estimate_id}/lines",
        json=[{"cost_catalog_item_id": catalog_item_id, "quantity": "10.00"}],
        headers=headers,
    )
    assert lines.status_code == 200, lines.text

    calc = await client.post(f"/estimates/{estimate_id}/calculate", headers=headers)
    assert calc.status_code == 200, calc.text

    send = await client.post(
        f"/estimates/{estimate_id}/send-for-signature",
        json={"signer_email": "client@example.test"},
        headers=headers,
    )
    assert send.status_code == 200, send.text

    files = {"signature_artifact": ("sig.png", b"fake-png-bytes", "image/png")}
    approve = await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": "Client Signer", "signer_email": "client@example.test"},
        files=files,
    )
    assert approve.status_code == 200, approve.text
    return estimate_id, calc.json()["total"]


async def _fetch_invoices_for_estimate(estimate_id):
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        return await conn.fetch("SELECT * FROM invoices WHERE estimate_id = $1", estimate_id)
    finally:
        await conn.close()


async def test_approving_an_estimate_with_a_project_drafts_a_deposit_invoice(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Deposit Co", "deposit-1@example.test")
    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    estimate_id, total = await _create_and_approve_estimate(
        client, admin["headers"], project["id"], markup_profile_id, catalog_item_id
    )

    invoices = await _fetch_invoices_for_estimate(estimate_id)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice["status"] == "draft"
    assert invoice["project_id"] == uuid.UUID(project["id"])
    assert invoice["due_date"] is None
    assert Decimal(invoice["amount"]) == Decimal(total) * Decimal("0.10")
```

Add a second test asserting the `None`-`project_id` no-op case. `app/schemas/estimate.py`'s `EstimateCreateRequest` confirms `project_id`/`lead_id` are both optional at the schema layer, with the router enforcing "at least one" — so an Estimate built with `lead_id` set and `project_id` omitted is a real, reachable path:

```python
async def _create_lead(client, headers):
    response = await client.post(
        "/leads",
        json={
            "contact_name": "Bare Lead Contact",
            "project_name": "Bare Lead Job",
            "email": "leadcontact@example.test",
            "project_type": "residential",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_approving_an_estimate_against_a_bare_lead_does_not_create_an_invoice(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Deposit Co 2", "deposit-2@example.test")
    lead = await _create_lead(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    create = await client.post(
        "/estimates",
        json={"lead_id": lead["id"], "markup_profile_id": markup_profile_id},
        headers=admin["headers"],
    )
    assert create.status_code == 201, create.text
    estimate_id = create.json()["id"]

    lines = await client.put(
        f"/estimates/{estimate_id}/lines",
        json=[{"cost_catalog_item_id": catalog_item_id, "quantity": "5.00"}],
        headers=admin["headers"],
    )
    assert lines.status_code == 200, lines.text
    calc = await client.post(f"/estimates/{estimate_id}/calculate", headers=admin["headers"])
    assert calc.status_code == 200, calc.text
    send = await client.post(
        f"/estimates/{estimate_id}/send-for-signature",
        json={"signer_email": "client2@example.test"},
        headers=admin["headers"],
    )
    assert send.status_code == 200, send.text

    files = {"signature_artifact": ("sig.png", b"fake-png-bytes", "image/png")}
    approve = await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": "Client Signer", "signer_email": "client2@example.test"},
        files=files,
    )
    assert approve.status_code == 200, approve.text

    invoices = await _fetch_invoices_for_estimate(estimate_id)
    assert invoices == [], "an Estimate with no project_id must not auto-generate an Invoice"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_estimate_approved_handler.py -v`
Expected: FAIL — no invoice gets created (zero registered handler for `ESTIMATE_APPROVED`)

- [ ] **Step 3: Write `backend/app/services/estimate_approved_handler.py`**

```python
"""ESTIMATE_APPROVED event handler (Task 3.39, design spec Section 2): drafts
a deposit Invoice the moment an Estimate is approved.

Registered against app.core.events by app.core.event_handlers's
register_event_handlers(), same is_registered() re-registration guard
handle_lead_won uses — see that module's own docstring for the full
rationale (the module-global handler registry is cleared before/after every
test by tests/conftest.py's autouse _clean_event_registry fixture).

Inherited Invariant #4: reuses the caller's session (the exact AsyncSession
approve_estimate's own route handler is using) and MUST NEVER call
session.commit()/rollback() itself — only flush().

project_id may be None (an Estimate approved against a bare Lead, no
Project yet) — this handler no-ops silently in that case (design spec
Section 2): invoices.project_id is NOT NULL, so there's nothing to create.
No retroactive invoice generation if that Estimate's Project is drafted
later — out of scope, would need its own trigger on project-creation.
"""
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice
from app.services.audit import write_audit_log
from app.services.invoicing import DEFAULT_DEPOSIT_PERCENTAGE, next_invoice_number


async def handle_estimate_approved(
    *,
    session: AsyncSession,
    estimate_id: uuid.UUID,
    project_id: uuid.UUID | None,
    company_id: uuid.UUID,
    approved_total: Decimal,
    **_ignored: object,
) -> None:
    if project_id is None:
        return

    invoice_number = await next_invoice_number(session, company_id)
    invoice = Invoice(
        id=uuid.uuid4(),
        project_id=project_id,
        company_id=company_id,
        estimate_id=estimate_id,
        invoice_number=invoice_number,
        amount=approved_total * DEFAULT_DEPOSIT_PERCENTAGE,
        status="draft",
        due_date=None,
    )
    session.add(invoice)
    await session.flush()

    await write_audit_log(
        session,
        company_id=company_id,
        actor_id=None,
        action="invoice.auto_generated",
        entity_type="invoice",
        entity_id=invoice.id,
        metadata={"estimate_id": str(estimate_id)},
    )
```

Check `app/routers/estimates.py`'s own `publish("ESTIMATE_APPROVED", ...)` call site for the exact actor/caller identity available at that point — if an `actor_id` (the client's e-signature, not an internal user) is more appropriate than `None` for the audit entry, use that instead; match whatever `estimate.approved`'s own audit entry already uses for consistency.

- [ ] **Step 4: Register the handler in `backend/app/core/event_handlers.py`**

```python
from app.services.estimate_approved_handler import handle_estimate_approved
```

Add to `register_event_handlers()`:

```python
    if not is_registered("ESTIMATE_APPROVED", handle_estimate_approved):
        register("ESTIMATE_APPROVED", handle_estimate_approved)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_estimate_approved_handler.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/estimate_approved_handler.py backend/app/core/event_handlers.py backend/tests/test_estimate_approved_handler.py
git commit -m "feat: wire ESTIMATE_APPROVED to auto-generate a draft deposit Invoice"
```

---

### Task 3.40: AR Tenant-Isolation Regression Tests

**Files:**
- Create: `backend/tests/test_invoicing_ap_tenant_isolation.py`

Following `test_billing_tenant_isolation.py`/`test_tenant_isolation_phase3.py`'s exact shape. This task covers the AR half; Task 3.43 extends the same file for AP.

- [ ] **Step 1: Write the tests**

```python
"""AR/AP tenant-isolation regression tests (design spec Section 9). AR half
first (Task 3.40); AP half appended by Task 3.43."""
import uuid

import asyncpg

from tests.conftest import TEST_DATABASE_URL

ADMIN_CONN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _create_project(client, headers):
    response = await client.post(
        "/projects", json={"name": "Iso Project", "site_address": "1 Main St", "status": "active"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_invoice(client, headers, project_id, amount="500.00"):
    response = await client.post(
        f"/projects/{project_id}/invoices", json={"amount": amount}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked_for_invoices(client):
    company_a = await _register_and_login(client, "Iso AR Co A", "iso-ar-a@example.test")
    company_b = await _register_and_login(client, "Iso AR Co B", "iso-ar-b@example.test")
    project_b = await _create_project(client, company_b["headers"])

    spoofed_headers = {**company_a["headers"], "X-Tenant-ID": company_b["company_id"]}
    response = await client.post(
        f"/projects/{project_b['id']}/invoices", json={"amount": "100.00"}, headers=spoofed_headers
    )
    assert response.status_code == 403


async def test_rls_policy_itself_blocks_cross_tenant_invoice_visibility(client):
    company_a = await _register_and_login(client, "Iso AR Co C", "iso-ar-c@example.test")
    company_b = await _register_and_login(client, "Iso AR Co D", "iso-ar-d@example.test")
    project_b = await _create_project(client, company_b["headers"])
    invoice_b = await _create_invoice(client, company_b["headers"], project_b["id"])

    response = await client.get(f"/invoices/{invoice_b['id']}", headers=company_a["headers"])
    assert response.status_code == 404


async def test_parent_admin_can_see_child_branch_invoice(client):
    parent = await _register_and_login(client, "Iso AR Parent", "iso-ar-parent@example.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": "Branch"}, headers=parent["headers"]
    )
    assert create_child.status_code == 201, create_child.text
    child_id = create_child.json()["id"]

    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}
    project = await _create_project(client, child_headers)
    invoice = await _create_invoice(client, child_headers, project["id"])

    response = await client.get(
        f"/invoices/{invoice['id']}", headers={**parent["headers"], "X-Tenant-ID": child_id}
    )
    assert response.status_code == 200, response.text


async def test_sibling_branches_cannot_see_each_others_invoices(client):
    parent = await _register_and_login(client, "Iso AR Parent 2", "iso-ar-parent2@example.test")
    child_a = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": "Branch A"}, headers=parent["headers"]
    )
    child_b = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": "Branch B"}, headers=parent["headers"]
    )
    child_a_id = child_a.json()["id"]
    child_b_id = child_b.json()["id"]

    headers_a = {**parent["headers"], "X-Tenant-ID": child_a_id}
    headers_b = {**parent["headers"], "X-Tenant-ID": child_b_id}
    project_a = await _create_project(client, headers_a)
    invoice_a = await _create_invoice(client, headers_a, project_a["id"])

    response = await client.get(f"/invoices/{invoice_a['id']}", headers=headers_b)
    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoicing_ap_tenant_isolation.py -v`
Expected: PASS, 4 passed

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_invoicing_ap_tenant_isolation.py
git commit -m "test: add AR tenant-isolation regression coverage for invoices"
```

---

### Task 3.41: POST/GET /bills, GET /bills/{id}

**Files:**
- Create: `backend/app/routers/bills.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_bills.py`

- [ ] **Step 1: Write the failing tests**

```python
async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _create_project(client, headers):
    response = await client.post(
        "/projects", json={"name": "Bill Project", "site_address": "1 Main St", "status": "active"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_subcontractor(client, headers):
    response = await client.post(
        "/subcontractors", json={"name": "Ace Plumbing", "trade": "plumbing"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_bill_against_a_subcontractor(client):
    admin = await _register_and_login(client, "Bill Co 1", "bill-1@example.test")
    project = await _create_project(client, admin["headers"])
    subcontractor = await _create_subcontractor(client, admin["headers"])

    response = await client.post(
        "/bills",
        json={"project_id": project["id"], "subcontractor_id": subcontractor["id"], "amount": "800.00"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "unpaid"
    assert body["outstanding_balance"] == "800.00"


async def test_create_bill_against_a_free_text_vendor_with_no_project(client):
    admin = await _register_and_login(client, "Bill Co 2", "bill-2@example.test")

    response = await client.post(
        "/bills", json={"vendor_name": "City Power & Light", "amount": "150.00"}, headers=admin["headers"]
    )
    assert response.status_code == 201, response.text
    assert response.json()["project_id"] is None


async def test_create_bill_with_neither_subcontractor_nor_vendor_name_returns_422(client):
    admin = await _register_and_login(client, "Bill Co 3", "bill-3@example.test")

    response = await client.post("/bills", json={"amount": "50.00"}, headers=admin["headers"])
    assert response.status_code == 422


async def test_project_manager_cannot_create_bill(client):
    admin = await _register_and_login(client, "Bill Co 4", "bill-4@example.test")
    invite = await client.post(
        "/invitations", json={"email": "pm-bill@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-bill@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.post(
        "/bills", json={"vendor_name": "Some Vendor", "amount": "50.00"}, headers=pm_headers
    )
    assert response.status_code == 403


async def test_client_cannot_read_bills(client):
    admin = await _register_and_login(client, "Bill Co 5", "bill-5@example.test")
    invite = await client.post(
        "/invitations", json={"email": "client-bill@example.test", "role": "client"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Client User", "password": "supersecret123"},
    )
    client_login = await client.post(
        "/auth/login", json={"email": "client-bill@example.test", "password": "supersecret123"}
    )
    client_headers = {"Authorization": f"Bearer {client_login.json()['access_token']}"}

    response = await client.get("/bills", headers=client_headers)
    assert response.status_code == 403


async def test_list_bills_filtered_by_project(client):
    admin = await _register_and_login(client, "Bill Co 6", "bill-6@example.test")
    project = await _create_project(client, admin["headers"])
    await client.post(
        "/bills", json={"project_id": project["id"], "vendor_name": "Vendor X", "amount": "10.00"}, headers=admin["headers"]
    )
    await client.post("/bills", json={"vendor_name": "Overhead Vendor", "amount": "20.00"}, headers=admin["headers"])

    scoped = await client.get(f"/bills?project_id={project['id']}", headers=admin["headers"])
    assert len(scoped.json()["items"]) == 1

    all_bills = await client.get("/bills", headers=admin["headers"])
    assert len(all_bills.json()["items"]) == 2


async def test_get_bill_detail_includes_empty_payments_list(client):
    admin = await _register_and_login(client, "Bill Co 7", "bill-7@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Y", "amount": "60.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    response = await client.get(f"/bills/{bill_id}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["payments"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_bills.py -v`
Expected: FAIL — routes don't exist yet

- [ ] **Step 3: Write `backend/app/routers/bills.py`**

```python
"""Task 3.41 (design spec Section 4): POST/GET /bills, GET /bills/{id}.

Top-level routes, not project-nested — bills.project_id is optional (company
overhead bills), unlike invoices which are always project-scoped. RBAC per
docs/07-security-compliance.md Section 2's "Accounting/Billing (AP)" row:
Admin/Accountant only, ZERO Client access — Bills are the company's own
internal obligations, never client-facing (unlike Invoices).
"""
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import Bill, BillPayment
from app.routers.projects import _get_project_or_404
from app.routers.subcontractors import _get_subcontractor_or_404
from app.schemas.bill import (
    BillCreateRequest,
    BillDetailResponse,
    BillListResponse,
    BillPaymentResponse,
    BillResponse,
)

router = APIRouter(prefix="/bills", tags=["bills"])

_ROLES = ("admin", "accountant")


async def _get_bill_or_404(current: CurrentUser, bill_id: uuid.UUID) -> Bill:
    result = await current.session.execute(select(Bill).where(Bill.id == bill_id))
    bill = result.scalar_one_or_none()
    if bill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bill not found")
    return bill


async def _paid_amount(current: CurrentUser, bill_id: uuid.UUID) -> Decimal:
    result = await current.session.execute(
        select(func.coalesce(func.sum(BillPayment.amount), 0)).where(BillPayment.bill_id == bill_id)
    )
    return result.scalar_one()


async def _bill_response(current: CurrentUser, bill: Bill) -> BillResponse:
    paid = await _paid_amount(current, bill.id)
    return BillResponse(
        id=bill.id,
        company_id=bill.company_id,
        project_id=bill.project_id,
        subcontractor_id=bill.subcontractor_id,
        vendor_name=bill.vendor_name,
        bill_number=bill.bill_number,
        amount=bill.amount,
        status=bill.status,
        due_date=bill.due_date,
        created_at=bill.created_at,
        outstanding_balance=bill.amount - paid,
    )


@router.post("", response_model=BillResponse, status_code=status.HTTP_201_CREATED)
async def create_bill(
    body: BillCreateRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> BillResponse:
    project_id: uuid.UUID | None = None
    if body.project_id is not None:
        project = await _get_project_or_404(current, body.project_id)
        project_id = project.id

    vendor_name = body.vendor_name
    if body.subcontractor_id is not None:
        subcontractor = await _get_subcontractor_or_404(current, body.subcontractor_id)
        vendor_name = vendor_name or subcontractor.name

    bill = Bill(
        id=uuid.uuid4(),
        company_id=current.company_id,
        project_id=project_id,
        subcontractor_id=body.subcontractor_id,
        vendor_name=vendor_name,
        bill_number=body.bill_number,
        amount=body.amount,
        status="unpaid",
        due_date=body.due_date,
    )
    current.session.add(bill)
    await current.session.flush()

    return await _bill_response(current, bill)


@router.get("", response_model=BillListResponse)
async def list_bills(
    current: CurrentUser = Depends(require_role(*_ROLES)),
    project_id: uuid.UUID | None = Query(None),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> BillListResponse:
    query = select(Bill)
    if project_id is not None:
        query = query.where(Bill.project_id == project_id)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Bill.created_at,
        id_col=Bill.id,
        cursor=cursor,
        limit=limit,
    )

    items = [await _bill_response(current, row) for row in rows]
    return BillListResponse(items=items, next_cursor=next_cursor)


@router.get("/{bill_id}", response_model=BillDetailResponse)
async def get_bill(
    bill_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_ROLES)),
) -> BillDetailResponse:
    bill = await _get_bill_or_404(current, bill_id)

    payments_result = await current.session.execute(
        select(BillPayment).where(BillPayment.bill_id == bill.id)
    )
    payments = [
        BillPaymentResponse.model_validate(payment) for payment in payments_result.scalars().all()
    ]

    base = await _bill_response(current, bill)
    return BillDetailResponse(**base.model_dump(), payments=payments)
```

Note: `subcontractor_id` given but `vendor_name` omitted resolves to the Subcontractor's own `name` at creation time (a denormalized snapshot, matching `estimate_line_items.unit_rate_snapshot`'s own historical-immutability convention) — if the Subcontractor's name changes later, this Bill's `vendor_name` does not retroactively change.

- [ ] **Step 4: Register the router in `backend/app/main.py`**

Add `from app.routers import bills` and `app.include_router(bills.router)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_bills.py -v`
Expected: PASS, 7 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/bills.py backend/app/main.py backend/tests/test_bills.py
git commit -m "feat: add POST/GET /bills, GET /bills/{id}"
```

---

### Task 3.42: POST /bills/{id}/payments, POST /bills/{id}/void

**Files:**
- Modify: `backend/app/routers/bills.py`
- Modify: `backend/tests/test_bills.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_bills.py`:

```python
async def test_cumulative_bill_payment_reaching_full_amount_auto_marks_paid(client):
    admin = await _register_and_login(client, "Bill Pay Co 1", "bill-pay-1@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Z", "amount": "300.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "100.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )
    second = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "200.00", "paid_date": "2026-08-02"}, headers=admin["headers"]
    )
    assert second.status_code == 201, second.text

    detail = await client.get(f"/bills/{bill_id}", headers=admin["headers"])
    body = detail.json()
    assert body["status"] == "paid"
    assert body["outstanding_balance"] == "0.00"


async def test_payment_against_void_bill_returns_409(client):
    admin = await _register_and_login(client, "Bill Pay Co 2", "bill-pay-2@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor W", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]
    await client.post(f"/bills/{bill_id}/void", headers=admin["headers"])

    response = await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "50.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )
    assert response.status_code == 409


async def test_void_an_unpaid_bill(client):
    admin = await _register_and_login(client, "Bill Void Co 1", "bill-void-1@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor V", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    response = await client.post(f"/bills/{bill_id}/void", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "void"


async def test_void_a_paid_bill_returns_409(client):
    admin = await _register_and_login(client, "Bill Void Co 2", "bill-void-2@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor U", "amount": "100.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]
    await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "100.00", "paid_date": "2026-08-01"}, headers=admin["headers"]
    )

    response = await client.post(f"/bills/{bill_id}/void", headers=admin["headers"])
    assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_bills.py -v -k "payment or void"`
Expected: FAIL — routes don't exist yet

- [ ] **Step 3: Add the routes to `backend/app/routers/bills.py`**

Add `BillPaymentCreateRequest` to the schema imports.

```python
@router.post(
    "/{bill_id}/payments", response_model=BillPaymentResponse, status_code=status.HTTP_201_CREATED
)
async def record_bill_payment(
    bill_id: uuid.UUID,
    body: BillPaymentCreateRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> BillPaymentResponse:
    bill = await _get_bill_or_404(current, bill_id)

    # Row-lock the bill for the rest of this transaction BEFORE reading its
    # status or computing the cumulative paid amount — same lost-update
    # race, same fix, as Task 3.37's record_invoice_payment (see that
    # route's own comment for the full explanation): without this,
    # concurrent payments against the SAME bill can each compute
    # _paid_amount() before either commits, and the cumulative-reaches-
    # amount check can pass for neither, leaving a fully-paid bill stuck
    # at "unpaid" forever.
    await current.session.execute(
        select(Bill.id).where(Bill.id == bill.id).with_for_update()
    )

    if bill.status == "void":
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot record a payment against a void bill")

    payment = BillPayment(
        id=uuid.uuid4(),
        bill_id=bill.id,
        company_id=bill.company_id,
        amount=body.amount,
        paid_date=body.paid_date,
        recorded_by=current.user.id,
    )
    current.session.add(payment)
    await current.session.flush()

    paid = await _paid_amount(current, bill.id)
    if paid >= bill.amount:
        bill.status = "paid"
        await current.session.flush()

    # docs/07-security-compliance.md Section 5 lists "Bill payment/void"
    # among the state changes requiring an audit_log row. paid_date is
    # included (not just payment_id) because it's user-supplied and has
    # real financial meaning — same rationale as Task 3.37's own
    # record_invoice_payment audit entry.
    await write_audit_log(
        current.session,
        company_id=bill.company_id,
        actor_id=current.user.id,
        action="bill.payment_recorded",
        entity_type="bill",
        entity_id=bill.id,
        metadata={
            "payment_id": str(payment.id),
            "amount": str(body.amount),
            "paid_date": body.paid_date.isoformat(),
        },
    )

    return BillPaymentResponse.model_validate(payment)


@router.post("/{bill_id}/void", response_model=BillResponse)
async def void_bill(
    bill_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> BillResponse:
    bill = await _get_bill_or_404(current, bill_id)
    if bill.status in ("paid", "void"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot void a {bill.status} bill")

    bill.status = "void"
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=bill.company_id,
        actor_id=current.user.id,
        action="bill.voided",
        entity_type="bill",
        entity_id=bill.id,
    )

    return await _bill_response(current, bill)
```

Add `from app.services.audit import write_audit_log` to `bills.py`'s imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_bills.py -v`
Expected: PASS — exact count depends on prior tasks' code-quality-review additions; check the actual current test count rather than a stale number.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/bills.py backend/tests/test_bills.py
git commit -m "feat: add POST /bills/{id}/payments, POST /bills/{id}/void"
```

---

### Task 3.43: AP Tenant-Isolation Regression Tests

**Files:**
- Modify: `backend/tests/test_invoicing_ap_tenant_isolation.py`

- [ ] **Step 1: Append the AP tests**

```python
async def _create_bill(client, headers, vendor_name="Vendor Iso", amount="200.00"):
    response = await client.post(
        "/bills", json={"vendor_name": vendor_name, "amount": amount}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked_for_bills(client):
    company_a = await _register_and_login(client, "Iso AP Co A", "iso-ap-a@example.test")
    company_b = await _register_and_login(client, "Iso AP Co B", "iso-ap-b@example.test")

    spoofed_headers = {**company_a["headers"], "X-Tenant-ID": company_b["company_id"]}
    response = await client.post(
        "/bills", json={"vendor_name": "Spoofed Vendor", "amount": "50.00"}, headers=spoofed_headers
    )
    assert response.status_code == 403


async def test_rls_policy_itself_blocks_cross_tenant_bill_visibility(client):
    company_a = await _register_and_login(client, "Iso AP Co C", "iso-ap-c@example.test")
    company_b = await _register_and_login(client, "Iso AP Co D", "iso-ap-d@example.test")
    bill_b = await _create_bill(client, company_b["headers"])

    response = await client.get(f"/bills/{bill_b['id']}", headers=company_a["headers"])
    assert response.status_code == 404


async def test_parent_admin_can_see_child_branch_bill(client):
    parent = await _register_and_login(client, "Iso AP Parent", "iso-ap-parent@example.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": "AP Branch"}, headers=parent["headers"]
    )
    assert create_child.status_code == 201, create_child.text
    child_id = create_child.json()["id"]

    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}
    bill = await _create_bill(client, child_headers)

    response = await client.get(f"/bills/{bill['id']}", headers=child_headers)
    assert response.status_code == 200, response.text


async def test_sibling_branches_cannot_see_each_others_bills(client):
    parent = await _register_and_login(client, "Iso AP Parent 2", "iso-ap-parent2@example.test")
    child_a = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": "AP Branch A"}, headers=parent["headers"]
    )
    child_b = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": "AP Branch B"}, headers=parent["headers"]
    )
    headers_a = {**parent["headers"], "X-Tenant-ID": child_a.json()["id"]}
    headers_b = {**parent["headers"], "X-Tenant-ID": child_b.json()["id"]}
    bill_a = await _create_bill(client, headers_a)

    response = await client.get(f"/bills/{bill_a['id']}", headers=headers_b)
    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_invoicing_ap_tenant_isolation.py -v`
Expected: PASS, 8 passed (4 AR from Task 3.40 + 4 AP)

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_invoicing_ap_tenant_isolation.py
git commit -m "test: add AP tenant-isolation regression coverage for bills"
```

---

### Task 3.44: POST/GET /projects/{id}/expenses

**Files:**
- Create: `backend/app/routers/expenses.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_expenses.py`

- [ ] **Step 1: Write the failing tests**

```python
async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def _create_project(client, headers):
    response = await client.post(
        "/projects", json={"name": "Expense Project", "site_address": "1 Main St", "status": "active"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_and_list_expenses(client):
    admin = await _register_and_login(client, "Expense Co", "expense-1@example.test")
    project = await _create_project(client, admin["headers"])

    create = await client.post(
        f"/projects/{project['id']}/expenses",
        json={"description": "Lumber run", "amount": "125.50", "incurred_on": "2026-08-01"},
        headers=admin["headers"],
    )
    assert create.status_code == 201, create.text

    listing = await client.get(f"/projects/{project['id']}/expenses", headers=admin["headers"])
    assert listing.status_code == 200, listing.text
    assert len(listing.json()["items"]) == 1
    assert listing.json()["items"][0]["description"] == "Lumber run"


async def test_project_manager_cannot_create_expense(client):
    admin = await _register_and_login(client, "Expense Co 2", "expense-2@example.test")
    project = await _create_project(client, admin["headers"])
    invite = await client.post(
        "/invitations", json={"email": "pm-expense@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-expense@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.post(
        f"/projects/{project['id']}/expenses",
        json={"description": "x", "amount": "1.00", "incurred_on": "2026-08-01"},
        headers=pm_headers,
    )
    assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_expenses.py -v`
Expected: FAIL — routes don't exist yet

- [ ] **Step 3: Write `backend/app/routers/expenses.py`**

```python
"""Task 3.44 (design spec Section 6): POST/GET /projects/{id}/expenses.
Unchanged in shape from the original pre-sketch — no lifecycle, no vendor
link, RBAC matches the same Admin/Accountant-only "Accounting/Billing" row
Bills use (docs/07-security-compliance.md Section 2).
"""
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import Expense
from app.routers.projects import _get_project_or_404
from app.schemas.expense import ExpenseCreateRequest, ExpenseListResponse, ExpenseResponse

router = APIRouter(tags=["expenses"])

_ROLES = ("admin", "accountant")


@router.post(
    "/projects/{project_id}/expenses",
    response_model=ExpenseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_expense(
    project_id: uuid.UUID,
    body: ExpenseCreateRequest,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> ExpenseResponse:
    project = await _get_project_or_404(current, project_id)

    expense = Expense(
        id=uuid.uuid4(),
        project_id=project.id,
        company_id=project.company_id,
        description=body.description,
        amount=body.amount,
        incurred_on=body.incurred_on,
    )
    current.session.add(expense)
    await current.session.flush()

    return ExpenseResponse.model_validate(expense)


@router.get("/projects/{project_id}/expenses", response_model=ExpenseListResponse)
async def list_expenses(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ExpenseListResponse:
    project = await _get_project_or_404(current, project_id)

    query = select(Expense).where(Expense.project_id == project.id)
    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Expense.incurred_on,
        id_col=Expense.id,
        cursor=cursor,
        limit=limit,
    )

    items = [ExpenseResponse.model_validate(row) for row in rows]
    return ExpenseListResponse(items=items, next_cursor=next_cursor)
```

Note on pagination: `paginate()`'s `created_at_col` type-hints a `datetime` column; `Expense.incurred_on` is a `Date`, not a `DateTime`. Verify this works (SQLAlchemy's `tuple_(...)` comparison and `.isoformat()`/`fromisoformat()` round-trip both work identically for `date` and `datetime` values) by running the test suite in Step 5 below — if `encode_cursor`/`decode_cursor` in `app/core/pagination.py` breaks on a plain `date` (its type hint says `datetime` specifically), that function needs a small widening (`datetime | date`) as part of this task, not a workaround in this router.

- [ ] **Step 4: Register the router in `backend/app/main.py`**

Add `from app.routers import expenses` and `app.include_router(expenses.router)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_expenses.py -v`
Expected: PASS, 2 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/expenses.py backend/app/main.py backend/tests/test_expenses.py
git commit -m "feat: add POST/GET /projects/{id}/expenses"
```

---

### Task 3.45: Overdue Scheduled Job + Scheduler Wiring

**Files:**
- Create: `backend/app/tasks/flag_overdue_financial_records.py`
- Modify: `backend/app/scheduler.py`
- Test: `backend/tests/test_flag_overdue_financial_records.py`

- [ ] **Step 1: Write the failing test**

```python
"""Task 3.45 (design spec Section 5). Tests the undecorated function
directly, same pattern tests/test_seat_usage_task.py uses for
_report_seat_usage."""
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

from app.tasks.flag_overdue_financial_records import _flag_overdue_financial_records
from tests.conftest import TEST_DATABASE_URL


async def test_flags_sent_invoice_past_due_date_as_overdue():
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)

    company_id = uuid.uuid4()
    project_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    try:
        async with session_factory() as session:
            await session.execute(
                text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Overdue Co')"),
                {"id": company_id},
            )
            await session.execute(
                text(
                    "INSERT INTO projects (id, company_id, name, site_address, status) "
                    "VALUES (:id, :cid, 'P', 'addr', 'active')"
                ),
                {"id": project_id, "cid": company_id},
            )
            await session.execute(
                text(
                    "INSERT INTO invoices (id, project_id, company_id, invoice_number, amount, status, due_date) "
                    "VALUES (:id, :pid, :cid, 'INV-TEST-0001', 100.00, 'sent', '2020-01-01')"
                ),
                {"id": invoice_id, "pid": project_id, "cid": company_id},
            )
            await session.commit()

        await _flag_overdue_financial_records(session_factory)

        async with session_factory() as session:
            result = await session.execute(
                text("SELECT status FROM invoices WHERE id = :id"), {"id": invoice_id}
            )
            assert result.scalar_one() == "overdue"
    finally:
        await owner_engine.dispose()


async def test_does_not_flag_paid_or_void_invoices_regardless_of_due_date():
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)

    company_id = uuid.uuid4()
    project_id = uuid.uuid4()
    paid_id = uuid.uuid4()
    void_id = uuid.uuid4()
    try:
        async with session_factory() as session:
            await session.execute(
                text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Overdue Co 2')"),
                {"id": company_id},
            )
            await session.execute(
                text(
                    "INSERT INTO projects (id, company_id, name, site_address, status) "
                    "VALUES (:id, :cid, 'P', 'addr', 'active')"
                ),
                {"id": project_id, "cid": company_id},
            )
            for inv_id, status in ((paid_id, "paid"), (void_id, "void")):
                await session.execute(
                    text(
                        "INSERT INTO invoices (id, project_id, company_id, invoice_number, amount, status, due_date) "
                        "VALUES (:id, :pid, :cid, :num, 100.00, :status, '2020-01-01')"
                    ),
                    {"id": inv_id, "pid": project_id, "cid": company_id, "num": f"INV-TEST-{status}", "status": status},
                )
            await session.commit()

        await _flag_overdue_financial_records(session_factory)

        async with session_factory() as session:
            result = await session.execute(
                text("SELECT id, status FROM invoices WHERE id IN (:paid_id, :void_id)"),
                {"paid_id": paid_id, "void_id": void_id},
            )
            statuses = {row.id: row.status for row in result}
            assert statuses[paid_id] == "paid"
            assert statuses[void_id] == "void"
    finally:
        await owner_engine.dispose()


async def test_flags_unpaid_bill_past_due_date_as_overdue():
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)

    company_id = uuid.uuid4()
    bill_id = uuid.uuid4()
    try:
        async with session_factory() as session:
            await session.execute(
                text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Overdue Bill Co')"),
                {"id": company_id},
            )
            await session.execute(
                text(
                    "INSERT INTO bills (id, company_id, vendor_name, amount, status, due_date) "
                    "VALUES (:id, :cid, 'Vendor', 100.00, 'unpaid', '2020-01-01')"
                ),
                {"id": bill_id, "cid": company_id},
            )
            await session.commit()

        await _flag_overdue_financial_records(session_factory)

        async with session_factory() as session:
            result = await session.execute(text("SELECT status FROM bills WHERE id = :id"), {"id": bill_id})
            assert result.scalar_one() == "overdue"
    finally:
        await owner_engine.dispose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_flag_overdue_financial_records.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write `backend/app/tasks/flag_overdue_financial_records.py`**

```python
"""Task 3.45 (design spec Section 5). Same undecorated-function/decorated-
actor split as app/tasks/compliance_expiry.py and app/tasks/seat_usage.py,
and the same owner-role engine pattern (legitimately cross-tenant — scans
every company's invoices/bills).

Two single, set-based UPDATEs in one transaction, not a per-row loop — no
per-row try/except like seat_usage.py's cross-subscription loop needs, since
neither UPDATE calls an external service that can fail mid-batch.

paid/void rows are excluded by each UPDATE's own WHERE clause status set — a
settled or voided record never gets flagged overdue regardless of due_date.
"""
from __future__ import annotations

import dramatiq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.tasks import broker  # noqa: F401 - import-time side effect

_owner_engine = create_async_engine(settings.migrations_database_url, pool_pre_ping=True)
_OwnerSessionLocal = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)


async def _flag_overdue_financial_records(
    session_factory: async_sessionmaker[AsyncSession] = _OwnerSessionLocal,
) -> None:
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE invoices SET status = 'overdue' "
                "WHERE status = 'sent' AND due_date < CURRENT_DATE"
            )
        )
        await session.execute(
            text(
                "UPDATE bills SET status = 'overdue' "
                "WHERE status = 'unpaid' AND due_date < CURRENT_DATE"
            )
        )
        await session.commit()


flag_overdue_financial_records = dramatiq.actor(
    max_retries=3, actor_name="flag_overdue_financial_records"
)(_flag_overdue_financial_records)
```

- [ ] **Step 4: Wire into `backend/app/scheduler.py`**

Add the import:

```python
from app.tasks.flag_overdue_financial_records import flag_overdue_financial_records
```

Add the wrapper function (same rationale as `_run_check_compliance_expiry`/`_run_report_seat_usage` — named, log-legible, independently unit-testable seam):

```python
def _run_flag_overdue_financial_records() -> None:
    flag_overdue_financial_records.send()
```

Add a third `add_job` call in the `if __name__ == "__main__":` block, at a distinct hour from the other two jobs:

```python
    scheduler.add_job(
        _run_flag_overdue_financial_records,
        trigger="cron",
        hour=4,
        misfire_grace_time=_MISFIRE_GRACE_TIME_SECONDS,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_flag_overdue_financial_records.py -v`
Expected: PASS, 3 passed

- [ ] **Step 6: Commit**

```bash
git add backend/app/tasks/flag_overdue_financial_records.py backend/app/scheduler.py backend/tests/test_flag_overdue_financial_records.py
git commit -m "feat: add unified overdue-flagging scheduled job for invoices and bills"
```

---

### Task 3.46: GET /reports/profitability

**Files:**
- Create: `backend/app/routers/reports.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: Write `backend/app/routers/reports.py`**

```python
"""Task 3.46 (design spec Section 7): GET /reports/profitability.

Per-project figures are date-range filtered on invoices.created_at/
bills.created_at; billed_revenue is what's actually been BILLED (status in
sent/paid/overdue), not the full Estimate total — a project with only a 10%
deposit invoiced shows that 10% as recognized revenue, matching accrual-
basis practice rather than overstating profitability on unbilled work.

AR/AP aging and tax_liability_estimate are point-in-time (today), not
date-range filtered — an aging report is inherently "as of now."
"""
import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.core.deps import CurrentUser, require_role
from app.models import Bill, BillPayment, Expense, Invoice, InvoicePayment
from app.schemas.profitability import AgingEntry, ProfitabilityReportResponse, ProjectProfitability
from app.services.invoicing import DEFAULT_TAX_RATE

router = APIRouter(prefix="/reports", tags=["reports"])

_ROLES = ("admin", "accountant")

_BILLED_STATUSES = ("sent", "paid", "overdue")
_AR_OUTSTANDING_STATUSES = ("sent", "overdue")
_AP_OUTSTANDING_STATUSES = ("unpaid", "overdue")


def _aging_bucket(due_date: date | None, today: date) -> str:
    if due_date is None:
        return "current"
    days_overdue = (today - due_date).days
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


@router.get("/profitability", response_model=ProfitabilityReportResponse)
async def get_profitability_report(
    current: CurrentUser = Depends(require_role(*_ROLES)),
    start_date: date = Query(...),
    end_date: date = Query(...),
) -> ProfitabilityReportResponse:
    revenue_result = await current.session.execute(
        select(Invoice.project_id, func.coalesce(func.sum(Invoice.amount), 0))
        .where(
            Invoice.status.in_(_BILLED_STATUSES),
            Invoice.created_at >= start_date,
            Invoice.created_at <= end_date,
        )
        .group_by(Invoice.project_id)
    )
    revenue_by_project: dict[uuid.UUID, Decimal] = dict(revenue_result.all())

    expense_result = await current.session.execute(
        select(Expense.project_id, func.coalesce(func.sum(Expense.amount), 0))
        .where(Expense.incurred_on >= start_date, Expense.incurred_on <= end_date)
        .group_by(Expense.project_id)
    )
    cost_by_project: dict[uuid.UUID, Decimal] = dict(expense_result.all())

    bill_result = await current.session.execute(
        select(Bill.project_id, func.coalesce(func.sum(Bill.amount), 0))
        .where(
            Bill.status != "void",
            Bill.project_id.isnot(None),
            Bill.created_at >= start_date,
            Bill.created_at <= end_date,
        )
        .group_by(Bill.project_id)
    )
    for project_id, amount in bill_result.all():
        cost_by_project[project_id] = cost_by_project.get(project_id, Decimal("0")) + amount

    all_project_ids = set(revenue_by_project) | set(cost_by_project)
    projects = [
        ProjectProfitability(
            project_id=project_id,
            billed_revenue=revenue_by_project.get(project_id, Decimal("0")),
            actual_cost=cost_by_project.get(project_id, Decimal("0")),
            profitability=revenue_by_project.get(project_id, Decimal("0"))
            - cost_by_project.get(project_id, Decimal("0")),
        )
        for project_id in all_project_ids
    ]

    today = date.today()

    ar_result = await current.session.execute(
        select(
            Invoice.id,
            Invoice.amount,
            Invoice.due_date,
            func.coalesce(func.sum(InvoicePayment.amount), 0).label("paid"),
        )
        .outerjoin(InvoicePayment, InvoicePayment.invoice_id == Invoice.id)
        .where(Invoice.status.in_(_AR_OUTSTANDING_STATUSES))
        .group_by(Invoice.id)
    )
    ar_aging = [
        AgingEntry(
            id=row.id,
            outstanding_balance=row.amount - row.paid,
            due_date=row.due_date,
            bucket=_aging_bucket(row.due_date, today),
        )
        for row in ar_result.all()
    ]

    ap_result = await current.session.execute(
        select(
            Bill.id,
            Bill.amount,
            Bill.due_date,
            func.coalesce(func.sum(BillPayment.amount), 0).label("paid"),
        )
        .outerjoin(BillPayment, BillPayment.bill_id == Bill.id)
        .where(Bill.status.in_(_AP_OUTSTANDING_STATUSES))
        .group_by(Bill.id)
    )
    ap_aging = [
        AgingEntry(
            id=row.id,
            outstanding_balance=row.amount - row.paid,
            due_date=row.due_date,
            bucket=_aging_bucket(row.due_date, today),
        )
        for row in ap_result.all()
    ]

    tax_liability_estimate = sum(revenue_by_project.values(), Decimal("0")) * DEFAULT_TAX_RATE

    return ProfitabilityReportResponse(
        projects=projects,
        ar_aging=ar_aging,
        ap_aging=ap_aging,
        tax_liability_estimate=tax_liability_estimate,
    )
```

- [ ] **Step 2: Register the router in `backend/app/main.py`**

Add `from app.routers import reports` and `app.include_router(reports.router)`.

- [ ] **Step 3: Verify the app starts cleanly**

Run: `cd backend && source .venv/Scripts/activate && python -c "from app.main import app; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/reports.py backend/app/main.py
git commit -m "feat: add GET /reports/profitability"
```

---

### Task 3.47: Profitability Report Tests

**Files:**
- Create: `backend/tests/test_profitability_report.py`

Fixture-driven, asserting exact `billed_revenue`/`actual_cost`/aging-bucket math against known seed data (design spec Section 9).

- [ ] **Step 1: Write the tests**

```python
"""Task 3.47 (design spec Section 9): fixture-driven profitability-report
tests against known seed data."""


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def _create_project(client, headers, name="Report Project"):
    response = await client.post(
        "/projects", json={"name": name, "site_address": "1 Main St", "status": "active"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_project_profitability_equals_billed_revenue_minus_expenses_and_bills(client):
    admin = await _register_and_login(client, "Report Co 1", "report-1@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "1000.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    await client.post(
        f"/projects/{project['id']}/expenses",
        json={"description": "Materials", "amount": "150.00", "incurred_on": "2026-08-05"},
        headers=admin["headers"],
    )
    await client.post(
        "/bills",
        json={"project_id": project["id"], "vendor_name": "Sub Co", "amount": "300.00"},
        headers=admin["headers"],
    )

    response = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    matching = [p for p in body["projects"] if p["project_id"] == project["id"]]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["billed_revenue"] == "1000.00"
    assert entry["actual_cost"] == "450.00"
    assert entry["profitability"] == "550.00"


async def test_draft_invoice_is_excluded_from_billed_revenue(client):
    admin = await _register_and_login(client, "Report Co 2", "report-2@example.test")
    project = await _create_project(client, admin["headers"])

    await client.post(
        f"/projects/{project['id']}/invoices", json={"amount": "1000.00"}, headers=admin["headers"]
    )

    response = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [p for p in body["projects"] if p["project_id"] == project["id"]]
    assert matching == [], "a draft invoice must not appear as billed revenue anywhere"


async def test_invoice_outside_date_range_is_excluded(client):
    admin = await _register_and_login(client, "Report Co 3", "report-3@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "1000.00", "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    response = await client.get(
        "/reports/profitability?start_date=2020-01-01&end_date=2020-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [p for p in body["projects"] if p["project_id"] == project["id"]]
    assert matching == []


async def test_ar_aging_bucket_for_a_sent_invoice_31_60_days_past_due(client):
    admin = await _register_and_login(client, "Report Co 4", "report-4@example.test")
    project = await _create_project(client, admin["headers"])

    from datetime import date, timedelta

    stale_due_date = (date.today() - timedelta(days=45)).isoformat()
    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "500.00", "due_date": stale_due_date},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])

    response = await client.get(
        "/reports/profitability?start_date=2020-01-01&end_date=2030-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [e for e in body["ar_aging"] if e["id"] == invoice_id]
    assert len(matching) == 1
    assert matching[0]["bucket"] == "31-60"
    assert matching[0]["outstanding_balance"] == "500.00"


async def test_ap_aging_reflects_partial_payment(client):
    admin = await _register_and_login(client, "Report Co 5", "report-5@example.test")

    create_bill = await client.post(
        "/bills",
        json={"vendor_name": "Aging Vendor", "amount": "400.00", "due_date": "2020-01-01"},
        headers=admin["headers"],
    )
    bill_id = create_bill.json()["id"]
    await client.post(
        f"/bills/{bill_id}/payments", json={"amount": "150.00", "paid_date": "2026-01-01"}, headers=admin["headers"]
    )

    response = await client.get(
        "/reports/profitability?start_date=2020-01-01&end_date=2030-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [e for e in body["ap_aging"] if e["id"] == bill_id]
    assert len(matching) == 1
    assert matching[0]["outstanding_balance"] == "250.00"
    assert matching[0]["bucket"] == "90+"


async def test_paid_invoice_is_excluded_from_ar_aging(client):
    admin = await _register_and_login(client, "Report Co 6", "report-6@example.test")
    project = await _create_project(client, admin["headers"])

    create_invoice = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "200.00", "due_date": "2020-01-01"},
        headers=admin["headers"],
    )
    invoice_id = create_invoice.json()["id"]
    await client.post(f"/invoices/{invoice_id}/send", json={}, headers=admin["headers"])
    await client.post(
        f"/invoices/{invoice_id}/payments", json={"amount": "200.00", "paid_date": "2026-01-01"}, headers=admin["headers"]
    )

    response = await client.get(
        "/reports/profitability?start_date=2020-01-01&end_date=2030-12-31", headers=admin["headers"]
    )
    body = response.json()
    matching = [e for e in body["ar_aging"] if e["id"] == invoice_id]
    assert matching == []


async def test_project_manager_cannot_read_profitability_report(client):
    admin = await _register_and_login(client, "Report Co 7", "report-7@example.test")
    invite = await client.post(
        "/invitations", json={"email": "pm-report@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-report@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=pm_headers
    )
    assert response.status_code == 403
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_profitability_report.py -v`
Expected: PASS, 7 passed

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_profitability_report.py
git commit -m "test: add fixture-driven profitability report coverage"
```

---

### Task 3.48: Read-Only Enforcement Verification

**Files:**
- None to create — this task verifies, not builds.

`block_if_read_only` was already wired into every write route in Tasks 3.35–3.37, 3.41–3.42, and 3.44 (`create_invoice`, `send_invoice`, `record_invoice_payment`, `void_invoice`, `create_bill`, `record_bill_payment`, `void_bill`, `create_expense`), not deferred to a separate retrofit task the way Billing's own routes needed (that dependency didn't exist yet when those routes were first built). This task just proves the existing completeness introspection test (`backend/tests/test_read_only_enforcement.py`, Task 3.28) picks up every new route automatically, with no changes needed to that test itself.

- [ ] **Step 1: Run the existing completeness test**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest tests/test_read_only_enforcement.py -v`
Expected: PASS — `test_every_write_route_has_block_if_read_only_except_deliberate_exclusions` passes with zero changes to that test file, because it walks `app.routes` live rather than a hand-maintained list.

- [ ] **Step 2: If it fails, fix the missing route, not the test**

If any new write route is missing `_ro: None = Depends(block_if_read_only)`, add it to that route (in `invoices.py`/`bills.py`/`expenses.py`) — do not add an exclusion for it. The only legitimate exclusions are the five already documented in that test's own docstring (`/auth/register`, `/auth/login`, `/webhooks/stripe`, `/subscriptions/portal-session`, `/invitations/{id}/accept`), none of which this plan adds a new instance of.

- [ ] **Step 3: No commit needed for this task** (verification only, no code changes expected). If Step 2 required a fix, commit that fix:

```bash
git add backend/app/routers/invoices.py backend/app/routers/bills.py backend/app/routers/expenses.py
git commit -m "fix: wire block_if_read_only into a write route missed during initial implementation"
```

---

### Task 3.49: Full-Stack E2E Extension

**Files:**
- Modify: `scripts/e2e_smoke_test.py`

Following the exact established pattern (Task 2.25, Task 3.13, Task 3.29's own Billing extension) — a new block after the Billing block, before the final frontend-health-check block.

- [ ] **Step 1: Add the new block**

Add a docstring note at the top of the file (matching the style of the existing Task 2.25/3.13/3.29 notes) explaining this block exercises the Invoicing/AR-AP flow: deposit-invoice auto-generation on Estimate approval, invoice send/payment/paid-transition, a vendor Bill with payment, and the profitability report reflecting both.

Register a new company ("E2E Company G" or the next letter in sequence — check what the Billing block used and continue from there), create a Project, a MarkupProfile, a CostCatalogItem, build and approve an Estimate (reusing the exact multi-step flow `test_estimate_approved_handler.py`'s own `_create_and_approve_estimate` helper establishes in Task 3.39 — check that helper's signature before duplicating logic). Assert:

1. After approval, `GET /projects/{id}/invoices` shows exactly one `draft` invoice with a `10%`-of-total `amount` and an `invoice_number` starting with `INV-`.
2. `POST /invoices/{id}/send` with a `due_date` succeeds, status becomes `sent`.
3. `POST /invoices/{id}/payments` for the full amount succeeds, status becomes `paid`.
4. `POST /bills` with a free-text vendor and an amount, then `POST /bills/{id}/payments` for the full amount, succeeds and status becomes `paid`.
5. `GET /reports/profitability` (with a wide date range covering "today") includes this project with `billed_revenue` matching the invoice amount and `actual_cost` matching the bill amount.

Follow the file's own established `checks_passed.append(...)` running-log pattern, `.example` email domains, and descriptive assertion failure messages exactly as prior blocks do.

- [ ] **Step 2: Run against the live local stack**

Bring up the full stack per the established procedure (`docker compose up -d --build`, verify via `docker exec ... pg_isready` not just `docker ps`, watch for stray processes per the documented Docker/Postgres stability discipline). Run `python scripts/e2e_smoke_test.py` and confirm all checks pass, including the new ones.

If the pre-existing, unrelated frontend-health-check issue flagged during Task 3.29 (port-mapping/`FRONTEND_URL` drift) is still unresolved, that's a separate known issue — don't let it block this task's own new checks from being verified; report it as still-open if encountered again, don't attempt to fix it here.

- [ ] **Step 3: Commit**

```bash
git add scripts/e2e_smoke_test.py
git commit -m "test: extend E2E smoke test with Invoicing/AR-AP flow (deposit invoice, payments, bill, profitability report)"
```

---

### Task 3.50: Full Regression Pass + Plan Closeout

**Files:**
- Modify: `docs/superpowers/specs/2026-07-14-invoicing-ar-ap-design.md`

- [ ] **Step 1: Full backend regression pass, twice solo**

Before running: check for stray Python processes (`wmic process where "name='python.exe'" get ProcessId,CommandLine,CreationDate` on Windows) and verify `docker exec <postgres-container> pg_isready -U postgres` succeeds — per the documented Docker/Postgres stability discipline, diagnose before assuming a hang if either run takes unusually long (a small file >1-2 min, or the full suite >20 min, is hung, not "still running").

Run: `cd backend && source .venv/Scripts/activate && python -m pytest -v` (twice, sequentially)
Expected: all tests pass both times, including every test added in Tasks 3.31–3.49.

- [ ] **Step 2: Dedicated RLS/tenant-isolation regression suite**

Run: `cd backend && source .venv/Scripts/activate && python -m pytest -v tests/test_tenant_isolation.py tests/test_rls_policy_regression.py tests/test_tenant_isolation_phase1.py tests/test_tenant_isolation_phase2.py tests/test_tenant_isolation_phase3.py tests/test_billing_tenant_isolation.py tests/test_invoicing_ap_tenant_isolation.py`
Expected: all pass — confirms no regression in any earlier phase's tables from this work.

- [ ] **Step 3: Update the design spec's Implementation Status note**

Add an "Implementation Status" note at the top of `docs/superpowers/specs/2026-07-14-invoicing-ar-ap-design.md` (right after the title, before "**Date:**"), matching the exact convention Billing's and Compliance Tracking's own specs use: state completion, the two regression-suite pass counts and timings, the E2E extension coverage, and any deliberately-not-fixed out-of-scope issue discovered along the way (if any).

- [ ] **Step 4: Commit the closeout**

```bash
git add docs/superpowers/specs/2026-07-14-invoicing-ar-ap-design.md
git commit -m "docs: close out Invoicing/AR-AP implementation"
```

- [ ] **Step 5: Push and open a PR against `main`**

```bash
git push -u origin feature/invoicing-expenses
gh pr create --base main --head feature/invoicing-expenses --title "feat: Invoicing/AR-AP - client invoices, vendor bills, expenses, profitability reporting" --body "..."
```

Confirm CI goes green. Merging remains an explicit, separate user decision — not automatic.
