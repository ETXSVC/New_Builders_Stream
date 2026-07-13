"""Task 3.8: `_check_compliance_expiry` background job.

Helper duplication (`_register_and_login`, `_create_subcontractor`,
`_upload_compliance_document`) follows the established per-test-file
convention (see test_compliance_dashboard.py, test_subcontractors.py) rather
than sharing them via conftest.py.

Database-used proof: `_check_compliance_expiry` defaults its `session_factory`
parameter to `app.tasks.compliance_expiry._OwnerSessionLocal`, itself built
from `settings.migrations_database_url` — which `tests/conftest.py` patches
(as an OS environment variable, before `app.config.settings` is ever
constructed) to point at the TEST database (`builders_stream_test`), not the
dev database. `test_owner_engine_points_at_test_database_not_dev_database`
below asserts this directly against the actual engine's URL, rather than
just trusting the tests to pass. Every other test in this file additionally
passes its OWN explicit `session_factory`, built the same way, straight from
`tests.conftest.TEST_DATABASE_URL` — the same constant `OWNER_DSN` in
test_tenant_isolation_phase2.py/test_tenant_isolation_phase3.py is derived
from — so each test's proof that it ran against the test database does not
even rely on that env-var-timing guarantee holding.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.compliance_notification import ComplianceNotification
from app.tasks.compliance_expiry import _check_compliance_expiry, _owner_engine
from tests.conftest import TEST_DATABASE_URL

_test_owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
_TestOwnerSessionLocal = async_sessionmaker(_test_owner_engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture(autouse=True)
async def _dispose_test_owner_engine():
    """Same Windows-ProactorEventLoop stale-pooled-connection rationale as
    conftest.py's own `_clean_tables` disposal of `app.db.engine`: this
    module creates its own separate SQLAlchemy engine
    (`_test_owner_engine`), so it needs its own disposal after every test."""
    yield
    await _test_owner_engine.dispose()


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
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    body = login.json()
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


async def _create_subcontractor(client, actor, **overrides):
    payload = {
        "name": "Ace Plumbing Co",
        "trade": "plumbing",
        "contact_email": "contact@aceplumbing.test",
    }
    payload.update(overrides)
    response = await client.post("/subcontractors", json=payload, headers=actor["headers"])
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _upload_compliance_document(
    client, actor, subcontractor_id, *, doc_type="insurance_certificate", expires_on,
    file_name="certificate.pdf", content: bytes = b"pdf-bytes",
):
    response = await client.post(
        f"/subcontractors/{subcontractor_id}/compliance-documents",
        data={"doc_type": doc_type, "expires_on": expires_on},
        files={"file": (file_name, content, "application/octet-stream")},
        headers=actor["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _fetch_notifications(company_id=None):
    async with _TestOwnerSessionLocal() as session:
        result = await session.execute(select(ComplianceNotification))
        notifications = result.scalars().all()
    if company_id is not None:
        notifications = [n for n in notifications if str(n.company_id) == company_id]
    return notifications


# =============================================================================
# Owner-role / test-database sanity check
# =============================================================================


def test_owner_engine_points_at_test_database_not_dev_database():
    """Direct, explicit proof (per this task's own self-review requirement)
    that the module's default `session_factory` — built from
    `settings.migrations_database_url` — resolves to the TEST database under
    pytest, not the dev database that URL points at outside of tests."""
    assert _owner_engine.url.database == "builders_stream_test"


# =============================================================================
# Single company, single document, single threshold crossing
# =============================================================================


async def test_document_5_days_out_fires_all_three_thresholds_simultaneously(client):
    admin = await _register_and_login(client, "Acme Construction", "expiry-single@acme.test")
    subcontractor_id = await _create_subcontractor(client, admin)
    expires_on = (date.today() + timedelta(days=5)).isoformat()
    document_id = await _upload_compliance_document(
        client, admin, subcontractor_id, expires_on=expires_on
    )

    await _check_compliance_expiry(session_factory=_TestOwnerSessionLocal)

    notifications = await _fetch_notifications(company_id=admin["company_id"])
    assert len(notifications) == 3  # 5 days out crosses 30_day, 14_day, and 7_day at once
    thresholds = {n.threshold for n in notifications}
    assert thresholds == {"30_day", "14_day", "7_day"}
    for notification in notifications:
        assert str(notification.compliance_document_id) == document_id
        assert str(notification.company_id) == admin["company_id"]
        assert notification.read_at is None


# =============================================================================
# Idempotency: re-run does not duplicate (exercises the UniqueConstraint)
# =============================================================================


async def test_second_run_does_not_duplicate_notification():
    """Uses a fixture-free, directly-constructed ComplianceDocument (owner
    connection, real company row) so two full runs of the actor against the
    exact same row are proven not to duplicate a notification — a second run
    producing the SAME row ids (not just the same row count, which could
    mask a duplicate-then-somehow-deduplicated bug) is the actual proof.

    Spec-compliance review of this task noted this test proves the
    application-level `already_fired` pre-check prevents the duplicate
    INSERT from ever being attempted — it does not itself trigger the DB
    `UniqueConstraint` (Task 3.1)'s own `IntegrityError` path, since this
    single-threaded, sequential test never races two concurrent scanner
    runs against each other. That constraint remains the actual backstop
    for a genuinely concurrent scenario (e.g. two overlapping scheduler
    runs); it just isn't the mechanism THIS particular test exercises."""
    from app.models.company import Company
    from app.models.compliance_document import ComplianceDocument
    from app.models.subcontractor import Subcontractor

    async with _TestOwnerSessionLocal() as session:
        company = Company(name="Acme Construction")
        session.add(company)
        await session.flush()

        subcontractor = Subcontractor(company_id=company.id, name="Ace Plumbing Co")
        session.add(subcontractor)
        await session.flush()

        document = ComplianceDocument(
            subcontractor_id=subcontractor.id,
            company_id=company.id,
            doc_type="insurance_certificate",
            storage_path="irrelevant/for/this/test.pdf",
            expires_on=date.today() + timedelta(days=5),
        )
        session.add(document)
        await session.commit()
        company_id, document_id = company.id, document.id

    await _check_compliance_expiry(session_factory=_TestOwnerSessionLocal)
    first_run_notifications = await _fetch_notifications(company_id=str(company_id))
    assert len(first_run_notifications) == 3

    await _check_compliance_expiry(session_factory=_TestOwnerSessionLocal)
    second_run_notifications = await _fetch_notifications(company_id=str(company_id))
    assert len(second_run_notifications) == 3

    # Same three rows, not new ones re-inserted alongside the originals.
    first_ids = {n.id for n in first_run_notifications}
    second_ids = {n.id for n in second_run_notifications}
    assert first_ids == second_ids
    for n in second_run_notifications:
        assert str(n.compliance_document_id) == str(document_id)


# =============================================================================
# Multi-company: no cross-tenant notification leakage
# =============================================================================


async def test_multiple_companies_no_cross_tenant_notification_leakage(client):
    company_a = await _register_and_login(client, "Company A", "expiry-a@acme.test")
    company_b = await _register_and_login(client, "Company B", "expiry-b@acme.test")

    sub_a = await _create_subcontractor(client, company_a, name="Company A Sub")
    sub_b = await _create_subcontractor(client, company_b, name="Company B Sub")

    expires_on = (date.today() + timedelta(days=5)).isoformat()
    document_a = await _upload_compliance_document(client, company_a, sub_a, expires_on=expires_on)
    document_b = await _upload_compliance_document(client, company_b, sub_b, expires_on=expires_on)

    await _check_compliance_expiry(session_factory=_TestOwnerSessionLocal)

    notifications_a = await _fetch_notifications(company_id=company_a["company_id"])
    notifications_b = await _fetch_notifications(company_id=company_b["company_id"])

    assert len(notifications_a) == 3
    assert len(notifications_b) == 3

    # Every row genuinely carries its OWN company's id and document id — not
    # just a count check. No row for company A ever carries company B's id
    # (or vice versa), and no row's document_id crosses over either.
    for notification in notifications_a:
        assert str(notification.company_id) == company_a["company_id"]
        assert str(notification.compliance_document_id) == document_a

    for notification in notifications_b:
        assert str(notification.company_id) == company_b["company_id"]
        assert str(notification.compliance_document_id) == document_b


# =============================================================================
# Exact threshold selection
# =============================================================================


async def test_document_25_days_out_fires_only_30_day_not_14_or_7(client):
    admin = await _register_and_login(client, "Acme Construction", "expiry-25days@acme.test")
    subcontractor_id = await _create_subcontractor(client, admin)
    expires_on = (date.today() + timedelta(days=25)).isoformat()
    document_id = await _upload_compliance_document(
        client, admin, subcontractor_id, expires_on=expires_on
    )

    await _check_compliance_expiry(session_factory=_TestOwnerSessionLocal)

    notifications = await _fetch_notifications(company_id=admin["company_id"])
    assert len(notifications) == 1
    assert notifications[0].threshold == "30_day"
    assert str(notifications[0].compliance_document_id) == document_id


# =============================================================================
# Later run: existing 30_day notification untouched, new 14_day fires
# =============================================================================


async def test_document_crossing_into_14_day_window_fires_only_new_notification_leaving_30_day_untouched(
    client,
):
    admin = await _register_and_login(client, "Acme Construction", "expiry-crossing@acme.test")
    subcontractor_id = await _create_subcontractor(client, admin)
    # 25 days out: first run fires only 30_day (per the test above).
    expires_on = (date.today() + timedelta(days=25)).isoformat()
    document_id = await _upload_compliance_document(
        client, admin, subcontractor_id, expires_on=expires_on
    )

    await _check_compliance_expiry(session_factory=_TestOwnerSessionLocal)
    first_run_notifications = await _fetch_notifications(company_id=admin["company_id"])
    assert len(first_run_notifications) == 1
    assert first_run_notifications[0].threshold == "30_day"
    original_30_day_id = first_run_notifications[0].id
    original_30_day_fired_at = first_run_notifications[0].fired_at

    # Move the document's expires_on back so it's now within the 14_day
    # window too (simulates the passage of time without needing to actually
    # wait days between test runs) — same owner-role direct-write approach
    # test_second_run_does_not_duplicate_notification uses.
    from app.models.compliance_document import ComplianceDocument

    new_expires_on = date.today() + timedelta(days=10)
    async with _TestOwnerSessionLocal() as session:
        result = await session.execute(
            select(ComplianceDocument).where(ComplianceDocument.id == document_id)
        )
        document = result.scalar_one()
        document.expires_on = new_expires_on
        await session.commit()

    await _check_compliance_expiry(session_factory=_TestOwnerSessionLocal)

    notifications = await _fetch_notifications(company_id=admin["company_id"])
    assert len(notifications) == 2
    thresholds = {n.threshold for n in notifications}
    assert thresholds == {"30_day", "14_day"}

    by_threshold = {n.threshold: n for n in notifications}

    # The existing 30_day row is the SAME row, not re-inserted or touched:
    # same id and same fired_at timestamp as the first run produced.
    assert by_threshold["30_day"].id == original_30_day_id
    assert by_threshold["30_day"].fired_at == original_30_day_fired_at

    # The new 14_day row is genuinely new, for the same document.
    assert str(by_threshold["14_day"].compliance_document_id) == document_id
