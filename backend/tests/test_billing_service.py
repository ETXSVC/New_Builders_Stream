import pytest

from app.config import settings
from app.services.billing import TIER_INCLUDED_SEATS, _build_stripe_client, get_stripe_client


def test_tier_included_seats_has_all_three_tiers():
    assert TIER_INCLUDED_SEATS == {"starter": 3, "pro": 10, "enterprise": 25}


def test_get_stripe_client_returns_a_fake_client_by_default():
    from app.services.stripe_client import FakeStripeClient

    assert isinstance(get_stripe_client(), FakeStripeClient)


def test_get_stripe_client_returns_the_same_instance_every_call():
    assert get_stripe_client() is get_stripe_client()


# --- Which client gets built, and on what ---------------------------------
#
# The whole switch is STRIPE_API_KEY. These call _build_stripe_client()
# directly rather than reimporting the module, because the singleton is
# built once at import time and every other test in the suite depends on it
# still being the fake.


def _set_stripe_env(monkeypatch, **values):
    defaults = {
        "stripe_api_key": None,
        "stripe_price_id_starter": None,
        "stripe_price_id_pro": None,
        "stripe_price_id_enterprise": None,
        "stripe_seat_overage_price_id": None,
        "stripe_portal_return_url": None,
    }
    defaults.update(values)
    for name, value in defaults.items():
        monkeypatch.setattr(settings, name, value)


def test_no_api_key_builds_the_fake(monkeypatch):
    from app.services.stripe_client import FakeStripeClient

    _set_stripe_env(monkeypatch)
    assert isinstance(_build_stripe_client(), FakeStripeClient)


def test_an_api_key_builds_the_real_client(monkeypatch):
    from app.services.stripe_client import RealStripeClient

    _set_stripe_env(
        monkeypatch,
        stripe_api_key="sk_test_x",
        stripe_price_id_starter="price_s",
        stripe_price_id_pro="price_p",
        stripe_price_id_enterprise="price_e",
    )
    client = _build_stripe_client()
    assert isinstance(client, RealStripeClient)
    assert client.tier_price_ids == {
        "starter": "price_s",
        "pro": "price_p",
        "enterprise": "price_e",
    }


def test_every_tier_in_tier_included_seats_needs_a_price(monkeypatch):
    """Adding a tier to TIER_INCLUDED_SEATS without a Price surfaces at boot
    as a named configuration error, not as a KeyError during that tier's
    first subscription."""
    from app.services.stripe_client import StripeConfigurationError

    _set_stripe_env(
        monkeypatch,
        stripe_api_key="sk_test_x",
        stripe_price_id_starter="price_s",
        stripe_price_id_pro="price_p",
        # enterprise deliberately unset
    )
    with pytest.raises(StripeConfigurationError) as exc:
        _build_stripe_client()
    assert "enterprise" in str(exc.value)


def test_portal_return_url_falls_back_to_the_frontend_base_url(monkeypatch):
    _set_stripe_env(
        monkeypatch,
        stripe_api_key="sk_test_x",
        stripe_price_id_starter="price_s",
        stripe_price_id_pro="price_p",
        stripe_price_id_enterprise="price_e",
    )
    client = _build_stripe_client()
    assert client.portal_return_url == settings.frontend_base_url


def test_a_test_mode_key_is_refused_in_production():
    """A sk_test_ key in production is the quiet failure: everything
    succeeds and nothing is ever charged."""
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError) as exc:
        Settings(
            app_env="production",
            database_url="postgresql+asyncpg://u:p@h/d",
            migrations_database_url="postgresql+asyncpg://u:p@h/d",
            test_database_url="postgresql+asyncpg://u:p@h/d",
            jwt_secret="x" * 40,
            stripe_webhook_secret="y" * 40,
            frontend_base_url="https://app.example.com",
            stripe_api_key="sk_test_abc123",
        )
    assert "sk_test" in str(exc.value)
