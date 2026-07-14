"""Task 3.23: _report_seat_usage (design spec Section 5). Same
undecorated-function/decorated-actor split as compliance_expiry.py
(app/tasks/compliance_expiry.py's own module docstring explains why:
Dramatiq wraps async actors in async_to_sync(), which needs a running
worker's event loop — tests call the undecorated coroutine directly
instead)."""
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Subscription
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


async def _get_subscription_id_for_company(company_id: str) -> str:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(Subscription.stripe_subscription_id).where(
                    Subscription.company_id == company_id
                )
            )
            return result.scalar_one()
    finally:
        await engine.dispose()


async def test_seats_in_a_child_branch_count_toward_the_root_subscription(client):
    """The entire point of get_all_descendant_ids over a flat company_id
    filter (module docstring): a user who only belongs to a DESCENDANT
    branch, never the root itself, must still count toward the root's
    subscription overage. Without this, the job would silently undercount
    every multi-branch company's real seat usage."""
    registered = await _register(client, "Branch Seats Co", "branch-seats@seat.test")
    root_id = registered["company_id"]
    login = await client.post(
        "/auth/login",
        json={"email": "branch-seats@seat.test", "password": "correct horse battery staple"},
    )
    admin_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    child = await client.post(
        f"/companies/{root_id}/children", json={"name": "Branch"}, headers=admin_headers
    )
    assert child.status_code == 201, child.text
    child_id = child.json()["id"]

    # 15 users in the CHILD branch only, none directly in the root — pushes
    # the root's total (1 root admin + 15 child-branch users = 16) 6 over
    # Pro's included_seats=10, exactly like the flat-company overage test
    # above, but every added user's own company_id is the child's, not the
    # root's.
    await _add_users_to_company(client, registered, child_id, 15, "branchuser")
    stripe_client = get_stripe_client()

    await _run_job()

    stripe_subscription_id = await _get_subscription_id_for_company(root_id)
    assert (stripe_subscription_id, 6) in stripe_client.reported_usage
