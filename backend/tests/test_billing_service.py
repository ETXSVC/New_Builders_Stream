from app.services.billing import TIER_INCLUDED_SEATS, get_stripe_client


def test_tier_included_seats_has_all_three_tiers():
    assert TIER_INCLUDED_SEATS == {"starter": 3, "pro": 10, "enterprise": 25}


def test_get_stripe_client_returns_a_fake_client_by_default():
    from app.services.stripe_client import FakeStripeClient

    assert isinstance(get_stripe_client(), FakeStripeClient)


def test_get_stripe_client_returns_the_same_instance_every_call():
    assert get_stripe_client() is get_stripe_client()
