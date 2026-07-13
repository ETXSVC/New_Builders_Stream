"""Task 3.17: FakeStripeClient — the only StripeClient implementation this
plan builds (design spec Section 2). Real Stripe SDK wiring is explicit
future work; every test in this whole plan uses this fake."""
import hashlib
import hmac
import json

import pytest

from app.services.stripe_client import FakeStripeClient, StripeSignatureError


async def test_create_customer_returns_a_stable_id():
    client = FakeStripeClient()
    customer_id = await client.create_customer(email="a@example.test", name="Acme Co")
    assert customer_id.startswith("cus_fake_")


async def test_create_trialing_subscription_returns_trialing_status():
    client = FakeStripeClient()
    sub = await client.create_trialing_subscription(
        customer_id="cus_fake_1", tier="pro", trial_days=14
    )
    assert sub.status == "trialing"
    assert sub.stripe_subscription_id.startswith("sub_fake_")
    assert sub.current_period_end is not None


async def test_create_portal_session_returns_a_url():
    client = FakeStripeClient()
    url = await client.create_portal_session(customer_id="cus_fake_1")
    assert url.startswith("https://")


async def test_report_seat_usage_records_the_call():
    client = FakeStripeClient()
    await client.report_seat_usage(stripe_subscription_id="sub_fake_1", quantity=3)
    assert client.reported_usage == [("sub_fake_1", 3)]


def test_verify_webhook_signature_accepts_a_correctly_signed_payload():
    client = FakeStripeClient()
    payload = json.dumps({"type": "customer.subscription.updated", "id": "evt_1"}).encode()
    signature = hmac.new(client.webhook_secret.encode(), payload, hashlib.sha256).hexdigest()

    event = client.verify_webhook_signature(payload=payload, signature_header=signature)

    assert event["type"] == "customer.subscription.updated"


def test_verify_webhook_signature_rejects_a_tampered_payload():
    client = FakeStripeClient()
    payload = json.dumps({"type": "customer.subscription.updated", "id": "evt_1"}).encode()
    signature = hmac.new(client.webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
    tampered_payload = json.dumps({"type": "customer.subscription.deleted", "id": "evt_1"}).encode()

    with pytest.raises(StripeSignatureError):
        client.verify_webhook_signature(payload=tampered_payload, signature_header=signature)
