"""Task 3.45 (design spec Section 5). Tests the undecorated function
directly, same pattern tests/test_seat_usage_task.py uses for
_report_seat_usage."""
import uuid

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


async def test_does_not_flag_paid_or_void_bills_regardless_of_due_date():
    """Mirrors test_does_not_flag_paid_or_void_invoices_regardless_of_
    due_date above, for bills — that test only covers invoices, this closes
    the equivalent AP gap."""
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)

    company_id = uuid.uuid4()
    paid_id = uuid.uuid4()
    void_id = uuid.uuid4()
    try:
        async with session_factory() as session:
            await session.execute(
                text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Overdue Bill Co 2')"),
                {"id": company_id},
            )
            for bid, status in ((paid_id, "paid"), (void_id, "void")):
                await session.execute(
                    text(
                        "INSERT INTO bills (id, company_id, vendor_name, amount, status, due_date) "
                        "VALUES (:id, :cid, 'Vendor', 100.00, :status, '2020-01-01')"
                    ),
                    {"id": bid, "cid": company_id, "status": status},
                )
            await session.commit()

        await _flag_overdue_financial_records(session_factory)

        async with session_factory() as session:
            result = await session.execute(
                text("SELECT id, status FROM bills WHERE id IN (:paid_id, :void_id)"),
                {"paid_id": paid_id, "void_id": void_id},
            )
            statuses = {row.id: row.status for row in result}
            assert statuses[paid_id] == "paid"
            assert statuses[void_id] == "void"
    finally:
        await owner_engine.dispose()


async def test_does_not_flag_records_due_today_or_in_the_future():
    """Nothing in the other tests proves the due_date < CURRENT_DATE
    boundary actually excludes today/future — every other test only ever
    uses a past date ('2020-01-01'). Covers both invoices and bills."""
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)

    company_id = uuid.uuid4()
    project_id = uuid.uuid4()
    invoice_today_id = uuid.uuid4()
    invoice_future_id = uuid.uuid4()
    bill_today_id = uuid.uuid4()
    bill_future_id = uuid.uuid4()
    try:
        async with session_factory() as session:
            await session.execute(
                text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Overdue Boundary Co')"),
                {"id": company_id},
            )
            await session.execute(
                text(
                    "INSERT INTO projects (id, company_id, name, site_address, status) "
                    "VALUES (:id, :cid, 'P', 'addr', 'active')"
                ),
                {"id": project_id, "cid": company_id},
            )
            for inv_id, due_date, num in (
                (invoice_today_id, "CURRENT_DATE", "INV-BOUND-TODAY"),
                (invoice_future_id, "'2099-01-01'", "INV-BOUND-FUTUR"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO invoices (id, project_id, company_id, invoice_number, amount, status, due_date) "
                        f"VALUES (:id, :pid, :cid, :num, 100.00, 'sent', {due_date})"
                    ),
                    {"id": inv_id, "pid": project_id, "cid": company_id, "num": num},
                )
            for bill_id, due_date in ((bill_today_id, "CURRENT_DATE"), (bill_future_id, "'2099-01-01'")):
                await session.execute(
                    text(
                        "INSERT INTO bills (id, company_id, vendor_name, amount, status, due_date) "
                        f"VALUES (:id, :cid, 'Vendor', 100.00, 'unpaid', {due_date})"
                    ),
                    {"id": bill_id, "cid": company_id},
                )
            await session.commit()

        await _flag_overdue_financial_records(session_factory)

        async with session_factory() as session:
            invoice_result = await session.execute(
                text("SELECT id, status FROM invoices WHERE id IN (:t, :f)"),
                {"t": invoice_today_id, "f": invoice_future_id},
            )
            invoice_statuses = {row.id: row.status for row in invoice_result}
            assert invoice_statuses[invoice_today_id] == "sent"
            assert invoice_statuses[invoice_future_id] == "sent"

            bill_result = await session.execute(
                text("SELECT id, status FROM bills WHERE id IN (:t, :f)"),
                {"t": bill_today_id, "f": bill_future_id},
            )
            bill_statuses = {row.id: row.status for row in bill_result}
            assert bill_statuses[bill_today_id] == "unpaid"
            assert bill_statuses[bill_future_id] == "unpaid"
    finally:
        await owner_engine.dispose()


async def test_does_not_flag_records_with_a_null_due_date():
    """NULL < CURRENT_DATE evaluates to NULL (SQL three-valued logic), not
    TRUE, in the WHERE clause — this proves that assumption empirically
    rather than leaving it untested."""
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)

    company_id = uuid.uuid4()
    project_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    bill_id = uuid.uuid4()
    try:
        async with session_factory() as session:
            await session.execute(
                text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Overdue Null Co')"),
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
                    "VALUES (:id, :pid, :cid, 'INV-NULL-DUE', 100.00, 'sent', NULL)"
                ),
                {"id": invoice_id, "pid": project_id, "cid": company_id},
            )
            await session.execute(
                text(
                    "INSERT INTO bills (id, company_id, vendor_name, amount, status, due_date) "
                    "VALUES (:id, :cid, 'Vendor', 100.00, 'unpaid', NULL)"
                ),
                {"id": bill_id, "cid": company_id},
            )
            await session.commit()

        await _flag_overdue_financial_records(session_factory)

        async with session_factory() as session:
            invoice_status = await session.execute(
                text("SELECT status FROM invoices WHERE id = :id"), {"id": invoice_id}
            )
            assert invoice_status.scalar_one() == "sent"

            bill_status = await session.execute(text("SELECT status FROM bills WHERE id = :id"), {"id": bill_id})
            assert bill_status.scalar_one() == "unpaid"
    finally:
        await owner_engine.dispose()
