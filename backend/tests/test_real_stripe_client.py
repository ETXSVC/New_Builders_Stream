"""RealStripeClient — the SDK-backed StripeClient (app/services/stripe_client.py).

No network. Every test either exercises pure logic (construction guards,
period-end resolution, signature verification) or swaps `_client` for a
recording stub after construction and asserts on the params that WOULD have
gone to Stripe. That is the part worth pinning: what this codebase asks
Stripe to do, not whether Stripe does it.

The stub is deliberately shaped like the real SDK's `v1` namespace rather
than being a bare Mock, so a rename in the call path (`client.customers` →
`client.v1.customers`, which is exactly what changed in stripe-python 15.x)
fails these tests instead of passing against a Mock that answers to
anything.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.stripe_client import (
    FakeStripeClient,
    RealStripeClient,
    StripeConfigurationError,
    StripeUnavailableError,
    build_stripe_signature_header,
)

PRICES = {"starter": "price_starter", "pro": "price_pro", "enterprise": "price_ent"}


def make_client(**overrides) -> RealStripeClient:
    kwargs = dict(
        api_key="sk_test_x",
        webhook_secret="whsec_test",
        tier_price_ids=dict(PRICES),
        portal_return_url="https://app.example.com/billing",
    )
    kwargs.update(overrides)
    return RealStripeClient(**kwargs)


# --------------------------------------------------------------------------
# Construction guards: every one of these is a boot failure by design.
# --------------------------------------------------------------------------


def test_missing_tier_price_refuses_to_construct():
    with pytest.raises(StripeConfigurationError) as exc:
        make_client(tier_price_ids={"starter": "price_s", "pro": "", "enterprise": "price_e"})
    # Names the tier AND the variable to set — a boot failure that makes the
    # operator go read the source is only half a boot failure.
    assert "pro" in str(exc.value)
    assert "STRIPE_PRICE_ID_PRO" in str(exc.value)


def test_missing_portal_return_url_refuses_to_construct():
    with pytest.raises(StripeConfigurationError):
        make_client(portal_return_url="")


def test_constructing_without_httpx_refuses_rather_than_failing_at_first_call(monkeypatch):
    """The trap this guard exists for: the SDK's *_async methods need httpx,
    httpx ships in the `dev` extra but not in the production image, so
    without this check the whole suite passes and the first real
    registration raises a bare ImportError from inside the SDK."""
    import builtins

    real_import = builtins.__import__

    def no_httpx(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError("No module named 'httpx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_httpx)
    with pytest.raises(StripeConfigurationError) as exc:
        make_client()
    assert "httpx" in str(exc.value)


# --------------------------------------------------------------------------
# A stub shaped like stripe.StripeClient().v1
# --------------------------------------------------------------------------


class RecordingService:
    def __init__(self, result=None):
        self.calls: list[tuple] = []
        self.result = result

    async def create_async(self, params=None, options=None):
        self.calls.append(("create", params, options))
        return self.result

    async def retrieve_async(self, ident, params=None, options=None):
        self.calls.append(("retrieve", ident, params))
        return self.result

    async def update_async(self, ident, params=None, options=None):
        self.calls.append(("update", ident, params))
        return self.result


def subscription_obj(*, sub_id="sub_1", status="trialing", trial_end=None, items=()):
    """A dict-and-attribute hybrid, because the real SDK's objects are both:
    `subscription.id` and `subscription["items"]` are each used in the code
    under test, and the real StripeObject supports both."""

    class Sub(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError:
                raise AttributeError(name)

    return Sub(
        id=sub_id,
        status=status,
        trial_end=trial_end,
        items=SimpleNamespace(data=list(items)),
    )


def item_obj(*, item_id, price_id, period_end=None):
    return SimpleNamespace(
        id=item_id,
        price=SimpleNamespace(id=price_id),
        current_period_end=period_end,
    )


def stub_into(client: RealStripeClient, **services) -> SimpleNamespace:
    v1 = SimpleNamespace(**services)
    client._client = SimpleNamespace(v1=v1)
    return v1


# --------------------------------------------------------------------------
# create_customer / create_trialing_subscription
# --------------------------------------------------------------------------


async def test_create_customer_sends_email_and_name_and_an_idempotency_key():
    client = make_client()
    customers = RecordingService(result=SimpleNamespace(id="cus_123"))
    stub_into(client, customers=customers)

    result = await client.create_customer(email="a@example.com", name="Acme")

    assert result == "cus_123"
    _, params, options = customers.calls[0]
    assert params == {"email": "a@example.com", "name": "Acme"}
    # Without this, a user who submits registration twice accretes a Stripe
    # customer per attempt — auth.py calls Stripe before its transaction.
    assert options["idempotency_key"] == "create_customer:a@example.com"


async def test_create_trialing_subscription_uses_the_tier_price_and_trial_days():
    client = make_client()
    subs = RecordingService(
        result=subscription_obj(sub_id="sub_9", status="trialing", trial_end=1800000000)
    )
    stub_into(client, subscriptions=subs)

    result = await client.create_trialing_subscription(
        customer_id="cus_1", tier="pro", trial_days=14
    )

    _, params, options = subs.calls[0]
    assert params["customer"] == "cus_1"
    assert params["items"] == [{"price": "price_pro", "quantity": 1}]
    assert params["trial_period_days"] == 14
    # A trial ending with no card cancels rather than raising an invoice
    # nobody can pay — "canceled" is a state block_if_read_only handles.
    assert params["trial_settings"]["end_behavior"]["missing_payment_method"] == "cancel"
    assert options["idempotency_key"] == "create_trial:cus_1"

    assert result.stripe_subscription_id == "sub_9"
    assert result.status == "trialing"
    assert result.current_period_end.timestamp() == 1800000000


async def test_seat_overage_item_is_added_at_creation_with_quantity_zero():
    """So the nightly job only ever UPDATEs an item, never has to decide
    whether to create one."""
    client = make_client(seat_overage_price_id="price_seats")
    subs = RecordingService(result=subscription_obj(trial_end=1800000000))
    stub_into(client, subscriptions=subs)

    await client.create_trialing_subscription(customer_id="cus_1", tier="starter", trial_days=14)

    _, params, _ = subs.calls[0]
    assert params["items"] == [
        {"price": "price_starter", "quantity": 1},
        {"price": "price_seats", "quantity": 0},
    ]


async def test_period_end_falls_back_to_the_item_when_not_trialing():
    """`current_period_end` left the Subscription object in API version
    2025-03-31.basil and now lives on each subscription item."""
    client = make_client()
    subs = RecordingService(
        result=subscription_obj(
            status="active",
            trial_end=None,
            items=[item_obj(item_id="si_1", price_id="price_pro", period_end=1900000000)],
        )
    )
    stub_into(client, subscriptions=subs)

    result = await client.create_trialing_subscription(
        customer_id="cus_1", tier="pro", trial_days=0
    )
    assert result.current_period_end.timestamp() == 1900000000


async def test_period_end_missing_everywhere_raises_rather_than_guessing():
    client = make_client()
    subs = RecordingService(result=subscription_obj(status="active", trial_end=None, items=[]))
    stub_into(client, subscriptions=subs)

    with pytest.raises(StripeUnavailableError):
        await client.create_trialing_subscription(customer_id="cus_1", tier="pro", trial_days=0)


# --------------------------------------------------------------------------
# Portal
# --------------------------------------------------------------------------


async def test_create_portal_session_passes_the_configured_return_url():
    client = make_client()
    sessions = RecordingService(result=SimpleNamespace(url="https://portal.stripe.com/x"))
    stub_into(client, billing_portal=SimpleNamespace(sessions=sessions))

    url = await client.create_portal_session(customer_id="cus_1")

    assert url == "https://portal.stripe.com/x"
    _, params, _ = sessions.calls[0]
    assert params == {
        "customer": "cus_1",
        "return_url": "https://app.example.com/billing",
    }


# --------------------------------------------------------------------------
# Seat overage — the "set, not increment" decision
# --------------------------------------------------------------------------


async def test_report_seat_usage_sets_quantity_without_proration():
    """SET semantics. app/tasks/seat_usage.py recomputes the whole overage
    every night, so an incrementing API (Stripe's meter events) would bill
    ~30x by month end. Setting a licensed item's quantity is idempotent by
    construction."""
    client = make_client(seat_overage_price_id="price_seats")
    items = [
        item_obj(item_id="si_base", price_id="price_pro"),
        item_obj(item_id="si_over", price_id="price_seats"),
    ]
    subs = RecordingService(result=subscription_obj(items=items))
    sub_items = RecordingService()
    stub_into(client, subscriptions=subs, subscription_items=sub_items)

    await client.report_seat_usage(stripe_subscription_id="sub_1", quantity=4)

    # The overage item, not the base tier item.
    (_, item_id, params), = sub_items.calls
    assert item_id == "si_over"
    assert params == {"quantity": 4, "proration_behavior": "none"}


async def test_reporting_the_same_overage_twice_does_not_accumulate():
    client = make_client(seat_overage_price_id="price_seats")
    items = [item_obj(item_id="si_over", price_id="price_seats")]
    subs = RecordingService(result=subscription_obj(items=items))
    sub_items = RecordingService()
    stub_into(client, subscriptions=subs, subscription_items=sub_items)

    await client.report_seat_usage(stripe_subscription_id="sub_1", quantity=2)
    await client.report_seat_usage(stripe_subscription_id="sub_1", quantity=2)

    # Both writes SET 2. The daily job is a snapshot, so two runs in one
    # period must bill 2 seats, not 4.
    assert [params["quantity"] for _, _, params in sub_items.calls] == [2, 2]


async def test_report_seat_usage_is_a_no_op_without_an_overage_price():
    client = make_client(seat_overage_price_id=None)
    subs = RecordingService(result=subscription_obj())
    sub_items = RecordingService()
    stub_into(client, subscriptions=subs, subscription_items=sub_items)

    await client.report_seat_usage(stripe_subscription_id="sub_1", quantity=4)

    # Not even a retrieve: an account with no overage pricing runs the
    # nightly job harmlessly rather than logging a failure every night.
    assert subs.calls == []
    assert sub_items.calls == []


async def test_report_seat_usage_skips_a_subscription_without_the_overage_item():
    client = make_client(seat_overage_price_id="price_seats")
    subs = RecordingService(
        result=subscription_obj(items=[item_obj(item_id="si_base", price_id="price_pro")])
    )
    sub_items = RecordingService()
    stub_into(client, subscriptions=subs, subscription_items=sub_items)

    await client.report_seat_usage(stripe_subscription_id="sub_1", quantity=4)

    # Adding a billable line item to an existing subscription is not a
    # decision a nightly job makes unattended.
    assert sub_items.calls == []


# --------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------


async def test_stripe_errors_become_stripe_unavailable_error():
    """So nothing outside stripe_client.py imports the SDK to catch a
    failure — which is what lets `stripe` stay an optional extra."""
    import stripe

    client = make_client()

    class Failing(RecordingService):
        async def create_async(self, params=None, options=None):
            raise stripe.APIConnectionError("stripe is down")

    stub_into(client, customers=Failing())

    with pytest.raises(StripeUnavailableError):
        await client.create_customer(email="a@example.com", name="Acme")


# --------------------------------------------------------------------------
# Webhook signature: the real client must verify EXACTLY as the fake does
# --------------------------------------------------------------------------


def test_real_client_verifies_the_same_signature_scheme_the_fake_signs():
    """One implementation of the security check, exercised by the whole
    suite — not one that ships and one that is tested."""
    client = make_client(webhook_secret="shared_secret")
    payload = json.dumps({"type": "customer.subscription.updated"}).encode()
    header = FakeStripeClient(webhook_secret="shared_secret").sign(payload)

    event = client.verify_webhook_signature(payload=payload, signature_header=header)

    assert event["type"] == "customer.subscription.updated"


def test_real_client_rejects_a_signature_made_with_another_secret():
    from app.services.stripe_client import StripeSignatureError

    client = make_client(webhook_secret="the_real_secret")
    payload = b'{"type":"x"}'
    header = build_stripe_signature_header(
        payload=payload, secret="not_the_real_secret", timestamp=_now()
    )

    with pytest.raises(StripeSignatureError):
        client.verify_webhook_signature(payload=payload, signature_header=header)


def _now() -> int:
    from datetime import datetime, timezone

    return int(datetime.now(timezone.utc).timestamp())
