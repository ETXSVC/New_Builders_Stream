"""Task 4.4 (design spec Section 2): AccountingProviderClient Protocol +
FakeAccountingProviderClient, mirroring app/services/stripe_client.py's own
StripeClient/FakeStripeClient shape."""
import pytest

from app.services.accounting_client import (
    AccountingProviderError,
    FakeAccountingProviderClient,
    get_accounting_client,
)


async def test_get_authorization_url_returns_a_url_containing_the_state():
    client = FakeAccountingProviderClient(provider="quickbooks")
    url = await client.get_authorization_url(company_id="company-1", state="signed-state-token")
    assert url.startswith("https://")
    assert "signed-state-token" in url


async def test_exchange_code_for_tokens_returns_a_distinct_access_and_refresh_token():
    client = FakeAccountingProviderClient(provider="quickbooks")
    tokens = await client.exchange_code_for_tokens(code="fake-code")
    assert tokens.access_token != tokens.refresh_token
    assert tokens.access_token.startswith("access_fake_")
    assert tokens.refresh_token.startswith("refresh_fake_")


async def test_exchange_echoes_the_callback_account_id():
    """QuickBooks' realmId arrives on the callback, not in the token
    response, so the route has to forward it and the client has to keep
    it — a token with no realm addresses no company file."""
    client = FakeAccountingProviderClient(provider="quickbooks")
    tokens = await client.exchange_code_for_tokens(code="c", callback_account_id="realm-42")
    assert tokens.account_id == "realm-42"


async def test_refresh_returns_a_new_refresh_token_not_the_one_passed_in():
    """Both real providers ROTATE: the refresh token spent here stops
    working immediately. The fake rotates too, so a caller that forgets to
    persist the new pair fails its tests here rather than permanently
    breaking real connections in production."""
    client = FakeAccountingProviderClient(provider="quickbooks")
    tokens = await client.refresh_access_token(refresh_token="refresh_fake_original")
    assert tokens.refresh_token != "refresh_fake_original"
    assert tokens.access_token.startswith("access_fake_")
    assert client.refresh_calls == 1


async def test_push_invoice_accumulates_into_pushed_invoices_and_returns_an_external_id():
    client = FakeAccountingProviderClient(provider="quickbooks")
    external_id = await client.push_invoice(
        access_token="tok", account_id="acct", invoice={"invoice_number": "INV-2026-0001"}, idempotency_key="inv-1"
    )
    assert external_id
    assert client.pushed_invoices == [{"invoice_number": "INV-2026-0001"}]


async def test_push_expense_accumulates_into_pushed_expenses():
    client = FakeAccountingProviderClient(provider="quickbooks")
    await client.push_expense(
        access_token="tok", account_id="acct", expense={"description": "Materials"}, idempotency_key="exp-1"
    )
    assert client.pushed_expenses == [{"description": "Materials"}]


async def test_push_bill_accumulates_into_pushed_bills():
    client = FakeAccountingProviderClient(provider="quickbooks")
    await client.push_bill(
        access_token="tok", account_id="acct", bill={"vendor_name": "Ace Plumbing"}, idempotency_key="bill-1"
    )
    assert client.pushed_bills == [{"vendor_name": "Ace Plumbing"}]


async def test_should_fail_makes_every_push_raise_accounting_provider_error():
    client = FakeAccountingProviderClient(provider="quickbooks", should_fail=True)
    with pytest.raises(AccountingProviderError):
        await client.push_invoice(
            access_token="tok", account_id="acct", invoice={}, idempotency_key="inv-fail"
        )
    with pytest.raises(AccountingProviderError):
        await client.push_expense(
            access_token="tok", account_id="acct", expense={}, idempotency_key="exp-fail"
        )
    with pytest.raises(AccountingProviderError):
        await client.push_bill(
            access_token="tok", account_id="acct", bill={}, idempotency_key="bill-fail"
        )
    # should_fail must not have accumulated any of the failed pushes.
    assert client.pushed_invoices == []
    assert client.pushed_expenses == []
    assert client.pushed_bills == []


# =============================================================================
# Idempotency-key dedup (added alongside Task 4.12's own double-post fix,
# app/tasks/accounting_sync.py) — a repeat push_* call with the SAME
# idempotency_key must be treated as "already processed," not pushed again.
# =============================================================================


async def test_push_invoice_with_repeated_idempotency_key_does_not_push_twice():
    client = FakeAccountingProviderClient(provider="quickbooks")
    first_id = await client.push_invoice(
        access_token="tok", account_id="acct", invoice={"invoice_number": "INV-1"}, idempotency_key="same-key"
    )
    second_id = await client.push_invoice(
        access_token="tok", account_id="acct", invoice={"invoice_number": "INV-1"}, idempotency_key="same-key"
    )
    assert second_id == first_id, "a repeated idempotency_key must return the SAME external_id"
    assert client.pushed_invoices == [{"invoice_number": "INV-1"}], (
        "a repeated idempotency_key must not push the invoice a second time"
    )


async def test_push_invoice_with_repeated_idempotency_key_ignores_should_fail():
    """Once a key has been recorded as successfully synced, a LATER
    should_fail=True flip on the same client instance must not make the
    repeat call raise — the dedup check happens before the should_fail
    check, mirroring a real provider recognizing an already-processed
    idempotency key regardless of the provider's current health."""
    client = FakeAccountingProviderClient(provider="quickbooks")
    first_id = await client.push_invoice(
        access_token="tok", account_id="acct", invoice={"invoice_number": "INV-2"}, idempotency_key="same-key-2"
    )
    client.should_fail = True
    second_id = await client.push_invoice(
        access_token="tok", account_id="acct", invoice={"invoice_number": "INV-2"}, idempotency_key="same-key-2"
    )
    assert second_id == first_id


async def test_different_entity_types_do_not_share_idempotency_key_namespace():
    """The dedup key is (operation, idempotency_key), not idempotency_key
    alone — an invoice and a bill happening to share the same raw key
    (not expected in practice, since callers use each entity's own id, but
    not guaranteed distinct across types either) must not be treated as
    the same push."""
    client = FakeAccountingProviderClient(provider="quickbooks")
    await client.push_invoice(
        access_token="tok", account_id="acct", invoice={"invoice_number": "INV-3"}, idempotency_key="shared-key"
    )
    await client.push_bill(
        access_token="tok", account_id="acct", bill={"vendor_name": "Ace Plumbing"}, idempotency_key="shared-key"
    )
    assert client.pushed_invoices == [{"invoice_number": "INV-3"}]
    assert client.pushed_bills == [{"vendor_name": "Ace Plumbing"}]


async def test_get_accounting_client_returns_a_fake_regardless_of_provider_string():
    quickbooks_client = get_accounting_client("quickbooks")
    freshbooks_client = get_accounting_client("freshbooks")
    assert isinstance(quickbooks_client, FakeAccountingProviderClient)
    assert isinstance(freshbooks_client, FakeAccountingProviderClient)
    assert quickbooks_client.provider == "quickbooks"
    assert freshbooks_client.provider == "freshbooks"


# --- Which client gets built ------------------------------------------------
#
# Per-provider and independent: QuickBooks and FreshBooks are two OAuth
# applications with two separate approval processes, so one going live must
# not drag the other with it.


def test_a_quickbooks_client_id_selects_the_real_quickbooks_client(monkeypatch):
    from app.config import settings
    from app.services.quickbooks_client import RealQuickBooksClient

    monkeypatch.setattr(settings, "quickbooks_client_id", "cid")
    monkeypatch.setattr(settings, "quickbooks_client_secret", "secret")

    assert isinstance(get_accounting_client("quickbooks"), RealQuickBooksClient)


def test_configuring_quickbooks_leaves_freshbooks_on_the_fake(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "quickbooks_client_id", "cid")
    monkeypatch.setattr(settings, "quickbooks_client_secret", "secret")

    assert isinstance(get_accounting_client("freshbooks"), FakeAccountingProviderClient)


def test_a_client_id_without_a_secret_refuses_rather_than_falling_back(monkeypatch):
    """Silently using the fake because half the credentials are present
    would look exactly like a working integration that syncs nothing."""
    from app.config import settings
    from app.services.accounting_client import AccountingConfigurationError

    monkeypatch.setattr(settings, "quickbooks_client_id", "cid")
    monkeypatch.setattr(settings, "quickbooks_client_secret", None)

    with pytest.raises(AccountingConfigurationError):
        get_accounting_client("quickbooks")


def test_the_redirect_uri_matches_the_callback_route(monkeypatch):
    """A provider rejects the code exchange outright if this does not match
    the registered URI exactly, so it is derived from one setting rather
    than configured as a second one that can disagree."""
    from app.config import settings
    from app.services.accounting_client import _redirect_uri

    monkeypatch.setattr(settings, "integrations_redirect_base_url", None)
    monkeypatch.setattr(settings, "frontend_base_url", "https://app.example.com/")

    assert _redirect_uri("quickbooks") == (
        "https://app.example.com/integrations/quickbooks/callback"
    )
