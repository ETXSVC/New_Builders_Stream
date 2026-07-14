"""Task 3.23: _report_seat_usage (design spec Section 5). Same
undecorated-function/decorated-actor split as compliance_expiry.py
(app/tasks/compliance_expiry.py's own module docstring explains why:
Dramatiq wraps async actors in async_to_sync(), which needs a running
worker's event loop — tests call the undecorated coroutine directly
instead)."""
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.billing import get_stripe_client
from app.tasks.seat_usage import _report_seat_usage
from tests.conftest import TEST_DATABASE_URL


async def _register(client, company_name, email):
    response = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_email": email,
            "admin_password": "correct horse battery staple",
            "admin_full_name": "Admin",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _run_job():
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        await _report_seat_usage(session_factory=session_factory)
    finally:
        await engine.dispose()


async def _add_users_to_company(client, admin, company_id, count, prefix):
    """Adds `count` additional admin-role members directly, bypassing the
    invitation flow purely for test setup speed — same convention every
    other phase's own seed helpers use."""
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            for i in range(count):
                user_id = uuid.uuid4()
                await conn.execute(
                    text(
                        "INSERT INTO users (id, email, password_hash, full_name) "
                        "VALUES (:id, :email, 'x', :name)"
                    ),
                    {"id": user_id, "email": f"{prefix}{i}@seat.test", "name": f"Seat {i}"},
                )
                await conn.execute(
                    text(
                        "INSERT INTO company_users (company_id, user_id, role) "
                        "VALUES (:company_id, :user_id, 'field_crew')"
                    ),
                    {"company_id": company_id, "user_id": user_id},
                )
    finally:
        await engine.dispose()


async def test_no_usage_reported_when_under_included_seats(client):
    await _register(client, "Under Seats Co", "under-seats@seat.test")
    stripe_client = get_stripe_client()
    before = list(stripe_client.reported_usage)

    await _run_job()

    # A brand-new company (1 admin, included_seats=10 for Pro) is nowhere
    # near overage — no new usage records for it.
    assert stripe_client.reported_usage == before


async def test_overage_reported_when_over_included_seats(client):
    registered = await _register(client, "Over Seats Co", "over-seats@seat.test")
    # Pro tier's included_seats=10 (Task 3.18) — 15 more users pushes total
    # to 16, 6 over the included count.
    await _add_users_to_company(client, registered, registered["company_id"], 15, "seatuser")
    stripe_client = get_stripe_client()

    await _run_job()

    matching = [u for u in stripe_client.reported_usage if u[1] == 6]
    assert len(matching) >= 1
