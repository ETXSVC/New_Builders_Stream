"""Task 3.21: POST /webhooks/stripe (design spec Section 4). No JWT — a
public receiver, signature-verified instead. Uses FakeStripeClient's own
webhook_secret (Task 3.17) to construct real, correctly-signed test
payloads, exactly the way Stripe's own SDK test utilities work — no live
Stripe account needed."""
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Subscription
from app.services.billing import get_stripe_client
from tests.conftest import TEST_DATABASE_URL


def _sign(payload: bytes) -> str:
    """A real `t=...,v1=...` header, produced by the same code path
    production verifies — see app/services/stripe_client.py."""
    return get_stripe_client().sign(payload)


# --- Where current_period_end lives in the event payload ------------------
#
# Stripe moved it off the subscription object and onto each subscription
# item in API version 2025-03-31.basil. Which shape arrives depends on the
# API version pinned to the webhook endpoint in the Stripe dashboard — not
# something this codebase controls, and changeable without a deploy, so the
# handler accepts both. Reading only the old location would leave
# current_period_end frozen at whatever registration wrote, silently.


def test_period_end_reads_the_legacy_top_level_field():
    from app.routers.webhooks import _period_end_of_event_object

    assert _period_end_of_event_object({"current_period_end": 1900000000}) == 1900000000


def test_period_end_reads_the_subscription_item_when_the_top_level_field_is_gone():
    from app.routers.webhooks import _period_end_of_event_object

    obj = {"items": {"data": [{"current_period_end": 1900000000}]}}
    assert _period_end_of_event_object(obj) == 1900000000


def test_period_end_takes_the_latest_item_when_items_bill_on_different_cycles():
    """Which is why the field moved in the first place. The subscription is
    paid up until the last item lapses, and this column gates
    block_if_read_only."""
    from app.routers.webhooks import _period_end_of_event_object

    obj = {
        "items": {
            "data": [
                {"current_period_end": 1800000000},
                {"current_period_end": 1900000000},
            ]
        }
    }
    assert _period_end_of_event_object(obj) == 1900000000


def test_period_end_is_none_when_the_payload_carries_neither():
    from app.routers.webhooks import _period_end_of_event_object

    # None means "leave the column alone", which _sync_subscription already
    # does — not "write NULL over a real period end".
    assert _period_end_of_event_object({}) is None
    assert _period_end_of_event_object({"items": {"data": []}}) is None


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


async def test_unknown_subscription_id_is_acknowledged_and_ignored(client):
    payload = json.dumps(
        {
            "type": "customer.subscription.updated",
            "data": {"object": {"id": "sub_does_not_exist", "status": "active"}},
        }
    ).encode()

    response = await client.post(
        "/webhooks/stripe", content=payload, headers={"Stripe-Signature": _sign(payload)}
    )

    assert response.status_code == 200


async def test_validly_signed_non_json_body_is_rejected_not_a_500(client):
    payload = b"this is not json"

    response = await client.post(
        "/webhooks/stripe", content=payload, headers={"Stripe-Signature": _sign(payload)}
    )

    assert response.status_code == 400
