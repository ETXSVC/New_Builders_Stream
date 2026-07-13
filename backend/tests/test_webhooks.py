"""Task 3.21: POST /webhooks/stripe (design spec Section 4). No JWT — a
public receiver, signature-verified instead. Uses FakeStripeClient's own
webhook_secret (Task 3.17) to construct real, correctly-signed test
payloads, exactly the way Stripe's own SDK test utilities work — no live
Stripe account needed."""
import hashlib
import hmac
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Subscription
from app.services.billing import get_stripe_client
from tests.conftest import TEST_DATABASE_URL


def _sign(payload: bytes) -> str:
    client = get_stripe_client()
    return hmac.new(client.webhook_secret.encode(), payload, hashlib.sha256).hexdigest()


async def _register_and_get_subscription_id(client, email="webhook-admin@wh.test"):
    response = await client.post(
        "/auth/register",
        json={
            "company_name": "Webhook Test Co",
            "admin_email": email,
            "admin_password": "correct horse battery staple",
            "admin_full_name": "Webhook Admin",
        },
    )
    assert response.status_code == 201, response.text
    company_id = response.json()["company_id"]

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(Subscription).where(Subscription.company_id == company_id)
            )
            return result.scalar_one().stripe_subscription_id
    finally:
        await engine.dispose()


async def _fetch_status(stripe_subscription_id: str) -> str:
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(Subscription).where(
                    Subscription.stripe_subscription_id == stripe_subscription_id
                )
            )
            return result.scalar_one().status
    finally:
        await engine.dispose()


async def test_subscription_updated_event_syncs_status(client):
    sub_id = await _register_and_get_subscription_id(client)
    payload = json.dumps(
        {
            "type": "customer.subscription.updated",
            "data": {"object": {"id": sub_id, "status": "active", "current_period_end": None}},
        }
    ).encode()

    response = await client.post(
        "/webhooks/stripe", content=payload, headers={"Stripe-Signature": _sign(payload)}
    )

    assert response.status_code == 200, response.text
    assert await _fetch_status(sub_id) == "active"


async def test_subscription_deleted_event_sets_canceled(client):
    sub_id = await _register_and_get_subscription_id(client, email="wh-deleted@wh.test")
    payload = json.dumps(
        {"type": "customer.subscription.deleted", "data": {"object": {"id": sub_id}}}
    ).encode()

    response = await client.post(
        "/webhooks/stripe", content=payload, headers={"Stripe-Signature": _sign(payload)}
    )

    assert response.status_code == 200, response.text
    assert await _fetch_status(sub_id) == "canceled"


async def test_invoice_payment_failed_event_sets_past_due(client):
    sub_id = await _register_and_get_subscription_id(client, email="wh-failed@wh.test")
    payload = json.dumps(
        {"type": "invoice.payment_failed", "data": {"object": {"subscription": sub_id}}}
    ).encode()

    response = await client.post(
        "/webhooks/stripe", content=payload, headers={"Stripe-Signature": _sign(payload)}
    )

    assert response.status_code == 200, response.text
    assert await _fetch_status(sub_id) == "past_due"


async def test_unknown_event_type_is_acknowledged_and_ignored(client):
    payload = json.dumps({"type": "customer.created", "data": {"object": {}}}).encode()

    response = await client.post(
        "/webhooks/stripe", content=payload, headers={"Stripe-Signature": _sign(payload)}
    )

    assert response.status_code == 200


async def test_tampered_signature_is_rejected(client):
    payload = json.dumps(
        {"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_doesnotmatter"}}}
    ).encode()

    response = await client.post(
        "/webhooks/stripe", content=payload, headers={"Stripe-Signature": "0" * 64}
    )

    assert response.status_code == 400
