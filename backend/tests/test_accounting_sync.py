"""Task 4.12 (design spec Section 5): sync_financial_record — the actual
(fake) push to the connected provider, with per-record status tracking and
Dramatiq-driven retry. Tests the undecorated function directly, same
pattern tests/test_flag_overdue_financial_records.py uses for
_flag_overdue_financial_records."""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.accounting_client import AccountingProviderError
from app.services.token_encryption import encrypt_token
from app.tasks.accounting_sync import _sync_financial_record
from tests.conftest import TEST_DATABASE_URL


async def _seed_connection_only(session_factory, *, provider="quickbooks"):
    """Seeds a company and an active IntegrationConnection, but deliberately
    NO invoice row — reproduces the enqueue-before-commit race the
    accounting_sync.py module docstring documents: Task 4.11's event
    handler enqueues sync_financial_record from inside the triggering
    request's own not-yet-committed session, and Dramatiq's broker isn't
    part of that transaction, so this actor can genuinely run before (or
    after a rollback of) the entity's own commit."""
    company_id = uuid.uuid4()
    connection_id = uuid.uuid4()

    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Sync Actor Co')"),
            {"id": company_id},
        )
        await session.execute(
            text(
                "INSERT INTO integration_connections "
                "(id, company_id, provider, access_token_encrypted, refresh_token_encrypted) "
                "VALUES (:id, :cid, :provider, :access_token, :refresh_token)"
            ),
            {
                "id": connection_id,
                "cid": company_id,
                "provider": provider,
                "access_token": encrypt_token("fake-access-token"),
                "refresh_token": encrypt_token("fake-refresh-token"),
            },
        )
        await session.commit()

    return {"company_id": company_id, "connection_id": connection_id}


async def test_entity_not_found_writes_a_failed_sync_record_and_reraises(monkeypatch):
    """Regression test for a real gap found in Task 4.12's own code-quality
    review: the entity-record fetch originally happened before the
    try/except, so a NoResultFound (entity rolled back/never committed by
    the time this actor runs) propagated uncaught, no
    integration_sync_records row was ever written, and after Dramatiq
    exhausted max_retries the message was dropped with zero visibility —
    directly contradicting this module's own "never silently vanishes"
    claim. Moving the entity fetch inside the try/except (connection
    lookup stays outside, since without it there's no company_id/
    connection_id to attribute a failure to) closes this gap."""
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)
    try:
        seeded = await _seed_connection_only(session_factory)
        missing_invoice_id = uuid.uuid4()

        with pytest.raises(Exception):
            await _sync_financial_record(
                connection_id=str(seeded["connection_id"]),
                entity_type="invoice",
                entity_id=str(missing_invoice_id),
                session_factory=session_factory,
            )

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT status, attempt_count, last_error FROM integration_sync_records "
                    "WHERE connection_id = :cid AND entity_id = :eid"
                ),
                {"cid": seeded["connection_id"], "eid": missing_invoice_id},
            )
            row = result.fetchone()
        assert row is not None, "a failed sync record must be written even when the entity is missing"
        assert row.status == "failed"
        assert row.attempt_count == 1
        assert row.last_error is not None
    finally:
        await owner_engine.dispose()


async def _seed_connection_and_invoice(session_factory, *, provider="quickbooks"):
    """Seeds a company, a Project (invoices.project_id is NOT NULL), an
    Invoice, and an active IntegrationConnection, via raw SQL — mirrors
    test_flag_overdue_financial_records.py's own seeding style for a
    worker-actor test that must not go through the app's HTTP layer.

    access_token_encrypted/refresh_token_encrypted must be real Fernet
    ciphertext (via encrypt_token), not arbitrary placeholder text — the
    actor calls the real decrypt_token() (Task 4.3), which performs
    genuine Fernet validation and raises TokenDecryptionError on anything
    that isn't valid ciphertext. A literal placeholder like 'enc-access'
    is not valid urlsafe-base64 Fernet output, so decrypt_token would
    fail before the actor ever reaches push_invoice/push_bill in every
    test below — a real bug (in the same family as the monkeypatching
    gotcha) if left as a bare literal."""
    company_id = uuid.uuid4()
    project_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    connection_id = uuid.uuid4()

    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Sync Actor Co')"),
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
                "VALUES (:id, :pid, :cid, 'INV-TEST-0001', 500.00, 'draft', NULL)"
            ),
            {"id": invoice_id, "pid": project_id, "cid": company_id},
        )
        await session.execute(
            text(
                "INSERT INTO integration_connections "
                "(id, company_id, provider, access_token_encrypted, refresh_token_encrypted) "
                "VALUES (:id, :cid, :provider, :access_token, :refresh_token)"
            ),
            {
                "id": connection_id,
                "cid": company_id,
                "provider": provider,
                "access_token": encrypt_token("fake-access-token"),
                "refresh_token": encrypt_token("fake-refresh-token"),
            },
        )
        await session.commit()

    return {"company_id": company_id, "invoice_id": invoice_id, "connection_id": connection_id}


async def test_successful_push_sets_status_success(monkeypatch):
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)
    try:
        seeded = await _seed_connection_and_invoice(session_factory)

        from app.services import accounting_client

        fake_client = accounting_client.FakeAccountingProviderClient(provider="quickbooks")
        monkeypatch.setattr(accounting_client, "get_accounting_client", lambda provider: fake_client)

        await _sync_financial_record(
            connection_id=str(seeded["connection_id"]),
            entity_type="invoice",
            entity_id=str(seeded["invoice_id"]),
            session_factory=session_factory,
        )

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT status, attempt_count, last_error FROM integration_sync_records "
                    "WHERE connection_id = :cid AND entity_id = :eid"
                ),
                {"cid": seeded["connection_id"], "eid": seeded["invoice_id"]},
            )
            row = result.fetchone()
        assert row.status == "success"
        assert row.attempt_count == 1
        assert row.last_error is None
        assert fake_client.pushed_invoices == [
            {"invoice_number": "INV-TEST-0001", "amount": "500.00", "status": "draft"}
        ]
    finally:
        await owner_engine.dispose()


async def test_failed_push_sets_status_failed_with_last_error_and_reraises(monkeypatch):
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)
    try:
        seeded = await _seed_connection_and_invoice(session_factory)

        from app.services import accounting_client

        failing_client = accounting_client.FakeAccountingProviderClient(
            provider="quickbooks", should_fail=True
        )
        monkeypatch.setattr(accounting_client, "get_accounting_client", lambda provider: failing_client)

        with pytest.raises(AccountingProviderError):
            await _sync_financial_record(
                connection_id=str(seeded["connection_id"]),
                entity_type="invoice",
                entity_id=str(seeded["invoice_id"]),
                session_factory=session_factory,
            )

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT status, attempt_count, last_error FROM integration_sync_records "
                    "WHERE connection_id = :cid AND entity_id = :eid"
                ),
                {"cid": seeded["connection_id"], "eid": seeded["invoice_id"]},
            )
            row = result.fetchone()
        assert row.status == "failed"
        assert row.attempt_count == 1
        assert row.last_error is not None
    finally:
        await owner_engine.dispose()


async def test_a_second_invocation_increments_attempt_count(monkeypatch):
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(owner_engine, expire_on_commit=False, class_=AsyncSession)
    try:
        seeded = await _seed_connection_and_invoice(session_factory)

        from app.services import accounting_client

        fake_client = accounting_client.FakeAccountingProviderClient(provider="quickbooks")
        monkeypatch.setattr(accounting_client, "get_accounting_client", lambda provider: fake_client)

        for _ in range(2):
            await _sync_financial_record(
                connection_id=str(seeded["connection_id"]),
                entity_type="invoice",
                entity_id=str(seeded["invoice_id"]),
                session_factory=session_factory,
            )

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT attempt_count FROM integration_sync_records "
                    "WHERE connection_id = :cid AND entity_id = :eid"
                ),
                {"cid": seeded["connection_id"], "eid": seeded["invoice_id"]},
            )
            row = result.fetchone()
        assert row.attempt_count == 2
    finally:
        await owner_engine.dispose()
