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
    access_token, refresh_token = await client.exchange_code_for_tokens(code="fake-code")
    assert access_token != refresh_token
    assert access_token.startswith("access_fake_")
    assert refresh_token.startswith("refresh_fake_")


async def test_push_invoice_accumulates_into_pushed_invoices_and_returns_an_external_id():
    client = FakeAccountingProviderClient(provider="quickbooks")
    external_id = await client.push_invoice(
        access_token="tok", invoice={"invoice_number": "INV-2026-0001"}, idempotency_key="inv-1"
    )
    assert external_id
    assert client.pushed_invoices == [{"invoice_number": "INV-2026-0001"}]


async def test_push_expense_accumulates_into_pushed_expenses():
    client = FakeAccountingProviderClient(provider="quickbooks")
    await client.push_expense(
        access_token="tok", expense={"description": "Materials"}, idempotency_key="exp-1"
    )
    assert client.pushed_expenses == [{"description": "Materials"}]


async def test_push_bill_accumulates_into_pushed_bills():
    client = FakeAccountingProviderClient(provider="quickbooks")
    await client.push_bill(
        access_token="tok", bill={"vendor_name": "Ace Plumbing"}, idempotency_key="bill-1"
    )
    assert client.pushed_bills == [{"vendor_name": "Ace Plumbing"}]


async def test_should_fail_makes_every_push_raise_accounting_provider_error():
    client = FakeAccountingProviderClient(provider="quickbooks", should_fail=True)
    with pytest.raises(AccountingProviderError):
        await client.push_invoice(access_token="tok", invoice={}, idempotency_key="inv-fail")
    with pytest.raises(AccountingProviderError):
        await client.push_expense(access_token="tok", expense={}, idempotency_key="exp-fail")
    with pytest.raises(AccountingProviderError):
        await client.push_bill(access_token="tok", bill={}, idempotency_key="bill-fail")
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
        access_token="tok", invoice={"invoice_number": "INV-1"}, idempotency_key="same-key"
    )
    second_id = await client.push_invoice(
        access_token="tok", invoice={"invoice_number": "INV-1"}, idempotency_key="same-key"
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
        access_token="tok", invoice={"invoice_number": "INV-2"}, idempotency_key="same-key-2"
    )
    client.should_fail = True
    second_id = await client.push_invoice(
        access_token="tok", invoice={"invoice_number": "INV-2"}, idempotency_key="same-key-2"
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
        access_token="tok", invoice={"invoice_number": "INV-3"}, idempotency_key="shared-key"
    )
    await client.push_bill(
        access_token="tok", bill={"vendor_name": "Ace Plumbing"}, idempotency_key="shared-key"
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
