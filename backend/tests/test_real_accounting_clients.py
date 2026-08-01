"""RealQuickBooksClient / RealFreshBooksClient — the clients that write into
a tenant's actual accounting file.

No network. `httpx.AsyncClient` is replaced with a recorder that returns
canned responses, so what these tests pin is the part this codebase owns:
the exact request built for each provider, and which failures are treated as
"refresh the token and retry" versus "this will fail the same way again".

The recorder is shaped like httpx (an async context manager exposing
`.request`/`.post`) rather than a bare Mock, so a change in how the clients
call it fails these tests instead of passing against something that answers
to anything.
"""
from __future__ import annotations

import httpx
import pytest

from app.services.accounting_client import (
    AccountingAuthError,
    AccountingConfigurationError,
    AccountingProviderError,
)
from app.services.freshbooks_client import RealFreshBooksClient
from app.services.quickbooks_client import RealQuickBooksClient


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Recorder:
    """Stands in for httpx.AsyncClient. `handler(method, url, params, body)`
    returns the FakeResponse for each call; every call is recorded."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, params=None, json=None, headers=None, data=None):
        self.calls.append(
            {"method": method, "url": url, "params": params, "json": json, "headers": headers}
        )
        return self.handler(method, url, params, json if json is not None else data)

    async def post(self, url, data=None, json=None, headers=None):
        return await self.request("POST", url, None, json, headers, data)


def install(monkeypatch, handler) -> Recorder:
    recorder = Recorder(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: recorder)
    return recorder


def qb(**overrides) -> RealQuickBooksClient:
    kwargs = dict(
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://app.test/integrations/quickbooks/callback",
        environment="sandbox",
    )
    kwargs.update(overrides)
    return RealQuickBooksClient(**kwargs)


def fb(**overrides) -> RealFreshBooksClient:
    kwargs = dict(
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://app.test/integrations/freshbooks/callback",
    )
    kwargs.update(overrides)
    return RealFreshBooksClient(**kwargs)


# =============================================================================
# Construction
# =============================================================================


def test_quickbooks_without_a_secret_refuses_to_construct():
    with pytest.raises(AccountingConfigurationError) as exc:
        qb(client_secret="")
    assert "QUICKBOOKS_CLIENT_SECRET" in str(exc.value)


def test_quickbooks_rejects_an_unknown_environment():
    """Sandbox and production are entirely separate datasets — pointing a
    live tenant at the wrong host writes their invoices somewhere they will
    never look."""
    with pytest.raises(AccountingConfigurationError):
        qb(environment="staging")


def test_freshbooks_without_a_secret_refuses_to_construct():
    with pytest.raises(AccountingConfigurationError):
        fb(client_secret="")


# =============================================================================
# What each provider needs before a record can exist
# =============================================================================


def test_quickbooks_invoice_needs_a_customer_and_an_item():
    specs = qb().required_refs("invoice")
    assert [(s.payload_key, s.kind) for s in specs] == [
        ("customer_id", "customer"),
        ("item_id", "item"),
    ]


def test_freshbooks_invoice_needs_only_a_customer():
    """FreshBooks invoice lines are free text — no item catalogue, so no
    ItemRef. This divergence is the whole reason each client declares its
    own requirements instead of the sync actor assuming one shape."""
    specs = fb().required_refs("invoice")
    assert [(s.payload_key, s.kind) for s in specs] == [("customer_id", "customer")]


def test_quickbooks_expense_needs_both_an_expense_and_a_payment_account():
    specs = qb().required_refs("expense")
    assert [s.literal for s in specs] == ["Expense", "Bank"]


# =============================================================================
# QuickBooks OAuth
# =============================================================================


async def test_quickbooks_authorization_url_carries_the_signed_state():
    url = await qb().get_authorization_url(company_id="c1", state="signed-state")
    assert url.startswith("https://appcenter.intuit.com/connect/oauth2?")
    assert "state=signed-state" in url
    assert "com.intuit.quickbooks.accounting" in url
    assert "redirect_uri=https%3A%2F%2Fapp.test" in url


async def test_quickbooks_exchange_without_a_realm_id_fails_loudly(monkeypatch):
    """realmId arrives as a callback query parameter, never in the token
    response. Storing a token without it would leave a connection that
    cannot address any company file — discovered hours later as an opaque
    404."""
    install(monkeypatch, lambda *a: FakeResponse(200, {}))
    with pytest.raises(AccountingProviderError) as exc:
        await qb().exchange_code_for_tokens(code="abc", callback_account_id=None)
    assert "realmId" in str(exc.value)


async def test_quickbooks_exchange_keeps_the_realm_id(monkeypatch):
    install(
        monkeypatch,
        lambda *a: FakeResponse(200, {"access_token": "at", "refresh_token": "rt"}),
    )
    tokens = await qb().exchange_code_for_tokens(code="abc", callback_account_id="realm-9")
    assert (tokens.access_token, tokens.refresh_token, tokens.account_id) == (
        "at",
        "rt",
        "realm-9",
    )


async def test_quickbooks_refresh_returns_the_rotated_pair(monkeypatch):
    install(
        monkeypatch,
        lambda *a: FakeResponse(200, {"access_token": "at2", "refresh_token": "rt2"}),
    )
    tokens = await qb().refresh_access_token(refresh_token="rt1")
    assert (tokens.access_token, tokens.refresh_token) == ("at2", "rt2")


async def test_a_dead_refresh_token_is_an_auth_error_not_a_generic_one(monkeypatch):
    """So the sync actor does not respond to "your refresh token is gone"
    by trying to refresh again."""
    install(monkeypatch, lambda *a: FakeResponse(400, None, "invalid_grant"))
    with pytest.raises(AccountingAuthError):
        await qb().refresh_access_token(refresh_token="expired")


# =============================================================================
# QuickBooks entity resolution
# =============================================================================


async def test_resolving_an_existing_customer_does_not_create_one(monkeypatch):
    recorder = install(
        monkeypatch,
        lambda method, url, params, body: FakeResponse(
            200, {"QueryResponse": {"Customer": [{"Id": "58"}]}}
        ),
    )
    found = await qb().resolve_entity(
        access_token="at", account_id="realm", kind="customer", name="Acme Holdings"
    )
    assert found == "58"
    assert [c["method"] for c in recorder.calls] == ["GET"], "a hit must not POST"


async def test_resolving_a_missing_customer_creates_it(monkeypatch):
    def handler(method, url, params, body):
        if method == "GET":
            return FakeResponse(200, {"QueryResponse": {}})
        return FakeResponse(200, {"Customer": {"Id": "91"}})

    recorder = install(monkeypatch, handler)
    created = await qb().resolve_entity(
        access_token="at", account_id="realm", kind="customer", name="Acme Holdings"
    )
    assert created == "91"
    post = [c for c in recorder.calls if c["method"] == "POST"][0]
    assert post["json"] == {"DisplayName": "Acme Holdings"}
    assert post["url"].endswith("/realm/customer")


async def test_a_customer_name_with_an_apostrophe_is_escaped(monkeypatch):
    """"O'Brien Construction" is not an edge case in this industry. An
    unescaped quote here is both a broken query and a query-injection
    shape."""
    recorder = install(
        monkeypatch,
        lambda *a: FakeResponse(200, {"QueryResponse": {"Customer": [{"Id": "1"}]}}),
    )
    await qb().resolve_entity(
        access_token="at", account_id="realm", kind="customer", name="O'Brien Construction"
    )
    query = recorder.calls[0]["params"]["query"]
    assert r"O\'Brien Construction" in query


async def test_no_account_of_the_requested_type_is_a_clear_error(monkeypatch):
    """Accounts are looked up, never created — inventing entries in
    somebody's chart of accounts is a bookkeeping decision."""
    recorder = install(monkeypatch, lambda *a: FakeResponse(200, {"QueryResponse": {}}))
    with pytest.raises(AccountingProviderError) as exc:
        await qb().resolve_entity(
            access_token="at", account_id="realm", kind="account", name="Expense"
        )
    assert "Expense" in str(exc.value)
    assert all(c["method"] == "GET" for c in recorder.calls), "must never create an account"


# =============================================================================
# QuickBooks pushes
# =============================================================================


async def test_push_invoice_sends_the_refs_and_the_idempotency_key(monkeypatch):
    recorder = install(monkeypatch, lambda *a: FakeResponse(200, {"Invoice": {"Id": "1001"}}))
    external_id = await qb().push_invoice(
        access_token="at",
        account_id="realm",
        invoice={
            "amount": "500.00",
            "invoice_number": "INV-1",
            "customer_id": "58",
            "item_id": "7",
            "due_date": "2026-09-01",
        },
        idempotency_key="entity-uuid",
    )
    assert external_id == "1001"
    call = recorder.calls[0]
    # requestid is THE idempotency mechanism: the same value replays the
    # original response instead of creating a second invoice.
    assert call["params"]["requestid"] == "entity-uuid"
    assert call["json"]["CustomerRef"] == {"value": "58"}
    assert call["json"]["DocNumber"] == "INV-1"
    line = call["json"]["Line"][0]
    assert line["Amount"] == 500.00
    assert line["SalesItemLineDetail"]["ItemRef"] == {"value": "7"}


async def test_push_expense_posts_a_purchase(monkeypatch):
    """QuickBooks has no "Expense" resource — a recorded expense is a
    Purchase."""
    recorder = install(monkeypatch, lambda *a: FakeResponse(200, {"Purchase": {"Id": "2002"}}))
    await qb().push_expense(
        access_token="at",
        account_id="realm",
        expense={"amount": "75.00", "expense_account_id": "12", "payment_account_id": "34"},
        idempotency_key="e1",
    )
    call = recorder.calls[0]
    assert call["url"].endswith("/realm/purchase")
    assert call["json"]["AccountRef"] == {"value": "34"}
    assert call["json"]["Line"][0]["AccountBasedExpenseLineDetail"]["AccountRef"] == {"value": "12"}


async def test_a_401_from_the_api_is_an_auth_error(monkeypatch):
    """The one failure the sync actor handles differently — refresh and
    retry once."""
    install(monkeypatch, lambda *a: FakeResponse(401, None, "unauthorized"))
    with pytest.raises(AccountingAuthError):
        await qb().push_invoice(
            access_token="stale",
            account_id="realm",
            invoice={"amount": "1.00", "customer_id": "1", "item_id": "1"},
            idempotency_key="k",
        )


async def test_a_400_from_the_api_is_not_an_auth_error(monkeypatch):
    """A validation failure retried with the same payload fails the same
    way, so it must not trigger a refresh."""
    install(monkeypatch, lambda *a: FakeResponse(400, None, "DocNumber is not unique"))
    with pytest.raises(AccountingProviderError) as exc:
        await qb().push_invoice(
            access_token="at",
            account_id="realm",
            invoice={"amount": "1.00", "customer_id": "1", "item_id": "1"},
            idempotency_key="k",
        )
    assert not isinstance(exc.value, AccountingAuthError)


# =============================================================================
# FreshBooks
# =============================================================================


async def test_freshbooks_exchange_fetches_the_account_id(monkeypatch):
    """FreshBooks sends nothing on the callback; the account id has to be
    fetched afterwards. It is `account_id` from a business membership, NOT
    `business_id` — the accounting endpoints take only the former."""

    def handler(method, url, params, body):
        if "users/me" in url:
            return FakeResponse(
                200,
                {
                    "response": {
                        "business_memberships": [
                            {"business": {"id": 111, "business_id": 222, "account_id": "ABC123"}}
                        ]
                    }
                },
            )
        return FakeResponse(200, {"access_token": "at", "refresh_token": "rt"})

    install(monkeypatch, handler)
    tokens = await fb().exchange_code_for_tokens(code="c", callback_account_id=None)
    assert tokens.account_id == "ABC123"


async def test_freshbooks_exchange_without_a_membership_fails_loudly(monkeypatch):
    def handler(method, url, params, body):
        if "users/me" in url:
            return FakeResponse(200, {"response": {"business_memberships": []}})
        return FakeResponse(200, {"access_token": "at", "refresh_token": "rt"})

    install(monkeypatch, handler)
    with pytest.raises(AccountingProviderError) as exc:
        await fb().exchange_code_for_tokens(code="c")
    assert "account_id" in str(exc.value)


async def test_freshbooks_refresh_returns_the_rotated_pair(monkeypatch):
    install(
        monkeypatch,
        lambda *a: FakeResponse(200, {"access_token": "at2", "refresh_token": "rt2"}),
    )
    tokens = await fb().refresh_access_token(refresh_token="rt1")
    assert (tokens.access_token, tokens.refresh_token) == ("at2", "rt2")


async def test_freshbooks_reuses_a_record_already_carrying_the_key(monkeypatch):
    """FreshBooks has no provider-side idempotency, so a repeat push has to
    search for its own earlier one. Weaker than QuickBooks' requestid, and
    the reason that difference is documented rather than glossed."""
    recorder = install(
        monkeypatch,
        lambda method, url, params, body: FakeResponse(
            200,
            {
                "response": {
                    "result": {"invoices": [{"id": 77, "notes": "bs-sync:entity-uuid"}]}
                }
            },
        ),
    )
    external_id = await fb().push_invoice(
        access_token="at",
        account_id="acct",
        invoice={"amount": "10.00", "customer_id": "5"},
        idempotency_key="entity-uuid",
    )
    assert external_id == "77"
    assert all(c["method"] == "GET" for c in recorder.calls), "must not create a duplicate"


async def test_freshbooks_creates_an_invoice_tagged_with_the_key(monkeypatch):
    def handler(method, url, params, body):
        if method == "GET":
            return FakeResponse(200, {"response": {"result": {"invoices": []}}})
        return FakeResponse(200, {"response": {"result": {"invoice": {"id": 78}}}})

    recorder = install(monkeypatch, handler)
    external_id = await fb().push_invoice(
        access_token="at",
        account_id="acct",
        invoice={"amount": "10.00", "customer_id": "5", "invoice_number": "INV-9"},
        idempotency_key="entity-uuid",
    )
    assert external_id == "78"
    post = [c for c in recorder.calls if c["method"] == "POST"][0]
    invoice = post["json"]["invoice"]
    assert invoice["customerid"] == "5"
    # The key has to land in a field that comes back on search, or the
    # dedup above can never find it.
    assert "bs-sync:entity-uuid" in invoice["notes"]
    assert invoice["lines"][0]["unit_cost"]["amount"] == "10.00"


async def test_freshbooks_vendor_resolution_matches_on_vendor_name(monkeypatch):
    recorder = install(
        monkeypatch,
        lambda *a: FakeResponse(
            200,
            {"response": {"result": {"vendors": [{"vendorid": 5, "vendor_name": "Ace Plumbing"}]}}},
        ),
    )
    found = await fb().resolve_entity(
        access_token="at", account_id="acct", kind="vendor", name="Ace Plumbing"
    )
    assert found == "5"
    assert all(c["method"] == "GET" for c in recorder.calls)


async def test_freshbooks_missing_expense_category_is_a_clear_error(monkeypatch):
    install(
        monkeypatch,
        lambda *a: FakeResponse(200, {"response": {"result": {"categories": []}}}),
    )
    with pytest.raises(AccountingProviderError) as exc:
        await fb().resolve_entity(
            access_token="at", account_id="acct", kind="category", name="Other Expenses"
        )
    assert "Other Expenses" in str(exc.value)
