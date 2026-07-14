"""Task 3.33: invoice-numbering helper and deposit/tax placeholder constants
(design spec Section 1/2). Uses the owner-role `db_session` fixture
(tests/conftest.py) because per-company invoice numbering independence must
be provable across TWO different companies within a single test — an
app_user-role, tenant-scoped session can only ever see one company's rows at
a time (RLS), so it can't make that assertion.
"""
import uuid
from decimal import Decimal

from app.models import Company, Invoice, Project
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

    # Invoice.project_id carries a real, non-deferrable FK to projects.id
    # (migrations/versions/0012_invoicing_ar_ap_schema.py) — a random UUID
    # with no backing Project row fails at flush with a ForeignKeyViolation,
    # so a real Project row is required here, not just a random UUID.
    project = Project(
        id=uuid.uuid4(),
        company_id=company.id,
        name="Numbering Co 2 Project",
        site_address="1 Test St",
    )
    db_session.add(project)
    await db_session.flush()

    first = await next_invoice_number(db_session, company.id)
    invoice = Invoice(
        id=uuid.uuid4(),
        project_id=project.id,
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

    project_a = Project(
        id=uuid.uuid4(),
        company_id=company_a.id,
        name="Co A Project",
        site_address="1 Test St",
    )
    db_session.add(project_a)
    await db_session.flush()

    number_a = await next_invoice_number(db_session, company_a.id)
    invoice_a = Invoice(
        id=uuid.uuid4(),
        project_id=project_a.id,
        company_id=company_a.id,
        invoice_number=number_a,
        amount=Decimal("50.00"),
        status="draft",
    )
    db_session.add(invoice_a)
    await db_session.flush()

    number_b = await next_invoice_number(db_session, company_b.id)
    assert number_b.endswith("-0001"), "company B's own numbering must not be affected by company A's invoices"
