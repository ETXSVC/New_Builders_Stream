"""Task 3.17: FakeStripeClient — the only StripeClient implementation this
plan builds (design spec Section 2). Real Stripe SDK wiring is explicit
future work; every test in this whole plan uses this fake."""
import json
import time

import pytest

from app.services.stripe_client import (
    STRIPE_SIGNATURE_TOLERANCE_SECONDS,
    FakeStripeClient,
    StripeSignatureError,
    build_stripe_signature_header,
)


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


# =============================================================================
# Webhook signature verification
#
# POST /webhooks/stripe is public and unauthenticated apart from this
# signature, on a route that can move any tenant's subscription to `active`
# — so this check is the entire security boundary, and it verifies Stripe's
# REAL `t=...,v1=...` header format rather than a simplified stand-in.
# =============================================================================


def _payload(event_type="customer.subscription.updated"):
    return json.dumps({"type": event_type, "id": "evt_1"}).encode()


def test_verify_webhook_signature_accepts_a_correctly_signed_payload():
    client = FakeStripeClient()
    payload = _payload()

    event = client.verify_webhook_signature(
        payload=payload, signature_header=client.sign(payload)
    )

    assert event["type"] == "customer.subscription.updated"


def test_verify_webhook_signature_rejects_a_tampered_payload():
    client = FakeStripeClient()
    signature = client.sign(_payload())

    with pytest.raises(StripeSignatureError):
        client.verify_webhook_signature(
            payload=_payload("customer.subscription.deleted"), signature_header=signature
        )


def test_verify_webhook_signature_rejects_a_replayed_old_request():
    """The finding: without a timestamp inside the signed string and a
    window checked against it, a captured request stays valid forever."""
    client = FakeStripeClient()
    payload = _payload()
    stale = client.sign(
        payload, timestamp=int(time.time()) - STRIPE_SIGNATURE_TOLERANCE_SECONDS - 1
    )

    with pytest.raises(StripeSignatureError, match="tolerance"):
        client.verify_webhook_signature(payload=payload, signature_header=stale)


def test_verify_webhook_signature_accepts_a_request_inside_the_window():
    client = FakeStripeClient()
    payload = _payload()
    recent = client.sign(
        payload, timestamp=int(time.time()) - STRIPE_SIGNATURE_TOLERANCE_SECONDS + 30
    )

    assert client.verify_webhook_signature(payload=payload, signature_header=recent)


def test_verify_webhook_signature_rejects_a_far_future_timestamp():
    """Rejected as well as stale ones: a one-sided check would let a request
    stamped far ahead stay valid indefinitely."""
    client = FakeStripeClient()
    payload = _payload()
    ahead = client.sign(
        payload, timestamp=int(time.time()) + STRIPE_SIGNATURE_TOLERANCE_SECONDS + 60
    )

    with pytest.raises(StripeSignatureError, match="tolerance"):
        client.verify_webhook_signature(payload=payload, signature_header=ahead)


def test_verify_webhook_signature_rejects_a_moved_timestamp():
    """The timestamp is INSIDE the signed string, so editing `t=` to bring a
    stale request back into the window invalidates the HMAC."""
    client = FakeStripeClient()
    payload = _payload()
    stale_ts = int(time.time()) - STRIPE_SIGNATURE_TOLERANCE_SECONDS - 1
    stale = client.sign(payload, timestamp=stale_ts)
    forged = stale.replace(f"t={stale_ts}", f"t={int(time.time())}")

    with pytest.raises(StripeSignatureError, match="does not match"):
        client.verify_webhook_signature(payload=payload, signature_header=forged)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "nonsense",
        "v1=deadbeef",  # no timestamp
        "t=notanumber,v1=deadbeef",
        "t=1700000000",  # no signature
    ],
)
def test_verify_webhook_signature_rejects_malformed_headers(header):
    """Every input here is attacker-controlled on a public endpoint; each
    must raise StripeSignatureError (a 400) rather than an unhandled 500."""
    client = FakeStripeClient()

    with pytest.raises(StripeSignatureError):
        client.verify_webhook_signature(payload=_payload(), signature_header=header)


def test_verify_webhook_signature_accepts_any_of_several_rotated_signatures():
    """Stripe sends multiple v1 values while a secret is being rotated; any
    one matching is valid, or every rotation would drop webhooks."""
    client = FakeStripeClient()
    payload = _payload()
    timestamp = int(time.time())
    valid = client.sign(payload, timestamp=timestamp)
    old_secret_signature = build_stripe_signature_header(
        payload=payload, secret="a-previous-secret", timestamp=timestamp
    ).split("v1=")[1]

    combined = f"{valid},v1={old_secret_signature}"

    assert client.verify_webhook_signature(payload=payload, signature_header=combined)


def test_verify_webhook_signature_ignores_unknown_signature_schemes():
    """Unknown `vN=` schemes are ignored rather than rejected, so a scheme
    Stripe adds alongside v1 doesn't break delivery."""
    client = FakeStripeClient()
    payload = _payload()
    header = f"{client.sign(payload)},v0=somethingelse"

    assert client.verify_webhook_signature(payload=payload, signature_header=header)
