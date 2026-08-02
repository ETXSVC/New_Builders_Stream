"""RealFreshBooksClient — the AccountingProviderClient that writes into a
tenant's actual FreshBooks account.

Selected per-provider by `FRESHBOOKS_CLIENT_ID`; the fake stays the default.
Plain REST over httpx, for the same reasons as the QuickBooks client.

Where FreshBooks differs from QuickBooks, and each difference is load-bearing:

* **The account id must be fetched, not received.** QuickBooks hands back a
  `realmId` on the callback; FreshBooks gives you nothing and expects a
  follow-up call to `/auth/api/v1/users/me`. Note it is `account_id` from a
  business membership, NOT `business_id` — the two are different values and
  the accounting endpoints take only the former, which is the kind of
  mistake that 404s with no explanation.

* **Invoice lines are free text.** No item catalogue, so no `ItemRef` and
  no Item to create — `required_refs` for an invoice asks for a client and
  nothing else, which is exactly why each client declares its own
  requirements instead of the sync actor assuming QuickBooks' shape.

* **There is no provider-side idempotency.** QuickBooks replays a response
  for a repeated `requestid`; FreshBooks has no equivalent, so the
  `idempotency_key` cannot simply be forwarded. `_find_existing` is the
  honest substitute: a search for a record already carrying this key before
  creating one. That is a genuinely weaker guarantee — two pushes racing
  inside the search-then-create window would both create — and it is
  narrower than the docstring on `accounting_client` implies for providers
  generally. It is not a race the sync actor can currently lose (one
  Dramatiq message per record, retried in sequence, never concurrently),
  and the key is written into a field that comes back on search so a retry
  after a crash finds it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.accounting_client import (
    AccountingAuthError,
    AccountingConfigurationError,
    AccountingProviderError,
    AccountingTokens,
    RefSpec,
)

_AUTHORIZE_URL = "https://auth.freshbooks.com/oauth/authorize"
_TOKEN_URL = "https://api.freshbooks.com/auth/oauth/token"
_ME_URL = "https://api.freshbooks.com/auth/api/v1/users/me"
_API_ROOT = "https://api.freshbooks.com/accounting/account"

# Where the idempotency key is written so a retry can find its own earlier
# push. `notes` is a free-text field on all three record types that comes
# back on a list query — the only field with those two properties.
_KEY_PREFIX = "bs-sync:"

_REQUIRED_REFS: dict[str, tuple[RefSpec, ...]] = {
    # No item ref: FreshBooks invoice lines are free text.
    "invoice": (
        RefSpec(payload_key="customer_id", kind="customer", from_payload="customer_name"),
    ),
    "bill": (RefSpec(payload_key="vendor_id", kind="vendor", from_payload="vendor_name"),),
    # FreshBooks books an expense against a category rather than an account
    # pair. "Other Expenses" is the catch-all every FreshBooks account
    # ships with; a tenant that wants finer categories is asking for a
    # mapping UI, which is a feature and not a default.
    "expense": (
        RefSpec(payload_key="category_id", kind="category", literal="Other Expenses"),
    ),
    # A FreshBooks payment references the invoice by id and needs nothing
    # else resolved — no customer ref, unlike QuickBooks.
    "payment": (),
}


@dataclass
class RealFreshBooksClient:
    client_id: str
    client_secret: str
    redirect_uri: str

    def __post_init__(self) -> None:
        if not self.client_secret:
            raise AccountingConfigurationError(
                "FRESHBOOKS_CLIENT_ID is set, so the real FreshBooks client is in use, "
                "but FRESHBOOKS_CLIENT_SECRET is not set"
            )

    def required_refs(self, record_type: str) -> tuple[RefSpec, ...]:
        return _REQUIRED_REFS.get(record_type, ())

    # --- OAuth -----------------------------------------------------------

    async def get_authorization_url(self, *, company_id: str, state: str) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code_for_tokens(
        self, *, code: str, callback_account_id: str | None = None
    ) -> AccountingTokens:
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        access_token = payload["access_token"]
        return AccountingTokens(
            access_token=access_token,
            refresh_token=payload["refresh_token"],
            # callback_account_id is ignored: FreshBooks does not send one,
            # and the accounting endpoints need an account_id this is the
            # only way to learn.
            account_id=await self._fetch_account_id(access_token),
        )

    async def refresh_access_token(self, *, refresh_token: str) -> AccountingTokens:
        payload = await self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "redirect_uri": self.redirect_uri,
            }
        )
        # Rotates, like QuickBooks: the refresh token just used is now dead
        # and the caller must persist this new pair.
        return AccountingTokens(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
        )

    async def _fetch_account_id(self, access_token: str) -> str:
        payload = await self._request(
            "GET", _ME_URL, access_token=access_token, what="users/me"
        )
        memberships = payload.get("response", {}).get("business_memberships") or []
        for membership in memberships:
            account_id = (membership.get("business") or {}).get("account_id")
            if account_id:
                return str(account_id)
        raise AccountingProviderError(
            "FreshBooks returned no business membership with an account_id; the "
            "accounting API cannot be addressed without one"
        )

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(
                _TOKEN_URL, json=data, headers={"Accept": "application/json"}
            )
        if response.status_code in (400, 401, 403):
            raise AccountingAuthError(
                f"FreshBooks token request rejected ({response.status_code}): "
                f"{response.text[:200]}"
            )
        if response.status_code >= 400:
            raise AccountingProviderError(
                f"FreshBooks token request failed ({response.status_code}): "
                f"{response.text[:200]}"
            )
        return response.json()

    # --- Entity resolution ------------------------------------------------

    async def resolve_entity(
        self, *, access_token: str, account_id: str, kind: str, name: str
    ) -> str:
        if kind == "customer":
            found = await self._search(
                access_token=access_token,
                account_id=account_id,
                path="users/clients",
                collection="clients",
                match=lambda row: row.get("organization") == name,
            )
            if found is not None:
                return str(found["id"])
            created = await self._create(
                access_token=access_token,
                account_id=account_id,
                path="users/clients",
                body={"client": {"organization": name}},
                collection="client",
            )
            return str(created["id"])

        if kind == "vendor":
            found = await self._search(
                access_token=access_token,
                account_id=account_id,
                path="bills/vendors",
                collection="vendors",
                match=lambda row: row.get("vendor_name") == name,
            )
            if found is not None:
                return str(found["vendorid"])
            created = await self._create(
                access_token=access_token,
                account_id=account_id,
                path="bills/vendors",
                body={"vendor": {"vendor_name": name}},
                collection="vendor",
            )
            return str(created["vendorid"])

        if kind == "category":
            # Looked up, never created — same reasoning as the QuickBooks
            # client's account branch: inventing categories in somebody's
            # books is a bookkeeping decision.
            found = await self._search(
                access_token=access_token,
                account_id=account_id,
                path="expenses/categories",
                collection="categories",
                match=lambda row: row.get("category") == name,
            )
            if found is None:
                raise AccountingProviderError(
                    f"No FreshBooks expense category named {name!r} exists in this "
                    "account; one is required to post an expense"
                )
            return str(found["categoryid"])

        raise AccountingProviderError(f"Unknown entity kind for FreshBooks: {kind!r}")

    # --- Pushes -----------------------------------------------------------

    async def push_invoice(
        self, *, access_token: str, account_id: str, invoice: dict, idempotency_key: str
    ) -> str:
        existing = await self._find_existing(
            access_token=access_token,
            account_id=account_id,
            path="invoices/invoices",
            collection="invoices",
            id_field="id",
            key=idempotency_key,
        )
        if existing is not None:
            return existing

        body = {
            "invoice": {
                "customerid": invoice["customer_id"],
                "create_date": invoice.get("issue_date"),
                "notes": _tag(idempotency_key),
                "lines": [
                    {
                        "name": invoice.get("description") or "Construction Services",
                        "qty": 1,
                        "unit_cost": {"amount": str(invoice["amount"]), "code": "USD"},
                    }
                ],
            }
        }
        if invoice.get("invoice_number"):
            body["invoice"]["invoice_number"] = invoice["invoice_number"]
        if invoice.get("due_date"):
            body["invoice"]["due_offset_days"] = 0

        created = await self._create(
            access_token=access_token,
            account_id=account_id,
            path="invoices/invoices",
            body=body,
            collection="invoice",
        )
        return str(created["id"])

    async def push_expense(
        self, *, access_token: str, account_id: str, expense: dict, idempotency_key: str
    ) -> str:
        existing = await self._find_existing(
            access_token=access_token,
            account_id=account_id,
            path="expenses/expenses",
            collection="expenses",
            id_field="id",
            key=idempotency_key,
        )
        if existing is not None:
            return existing

        body = {
            "expense": {
                "amount": {"amount": str(expense["amount"]), "code": "USD"},
                "categoryid": expense["category_id"],
                "notes": _tag(idempotency_key, expense.get("description")),
            }
        }
        created = await self._create(
            access_token=access_token,
            account_id=account_id,
            path="expenses/expenses",
            body=body,
            collection="expense",
        )
        return str(created["id"])

    async def push_payment(
        self, *, access_token: str, account_id: str, payment: dict, idempotency_key: str
    ) -> str:
        """A payment against the invoice it settles.

        `invoiceid` is the invoice's FreshBooks id, supplied by the sync
        actor from that invoice's own sync record — a payment cannot sync
        before its invoice has.

        Note the dedup search is over `payments`, so the same
        search-before-create weakness as every other FreshBooks push applies
        (see the module docstring); the key is carried in `notes` for the
        same reason.
        """
        existing = await self._find_existing(
            access_token=access_token,
            account_id=account_id,
            path="payments/payments",
            collection="payments",
            id_field="id",
            key=idempotency_key,
        )
        if existing is not None:
            return existing

        body = {
            "payment": {
                "invoiceid": payment["external_invoice_id"],
                "amount": {"amount": str(payment["amount"]), "code": "USD"},
                "notes": _tag(idempotency_key),
            }
        }
        if payment.get("paid_date"):
            body["payment"]["date"] = payment["paid_date"]

        created = await self._create(
            access_token=access_token,
            account_id=account_id,
            path="payments/payments",
            body=body,
            collection="payment",
        )
        return str(created["id"])

    async def push_bill(
        self, *, access_token: str, account_id: str, bill: dict, idempotency_key: str
    ) -> str:
        existing = await self._find_existing(
            access_token=access_token,
            account_id=account_id,
            path="bills/bills",
            collection="bills",
            id_field="id",
            key=idempotency_key,
        )
        if existing is not None:
            return existing

        body = {
            "bill": {
                "vendorid": bill["vendor_id"],
                "notes": _tag(idempotency_key),
                "lines": [
                    {
                        "amount": {"amount": str(bill["amount"]), "code": "USD"},
                        "quantity": 1,
                    }
                ],
            }
        }
        if bill.get("due_date"):
            body["bill"]["due_date"] = bill["due_date"]

        created = await self._create(
            access_token=access_token,
            account_id=account_id,
            path="bills/bills",
            body=body,
            collection="bill",
        )
        return str(created["id"])

    # --- HTTP -------------------------------------------------------------

    async def _find_existing(
        self,
        *,
        access_token: str,
        account_id: str,
        path: str,
        collection: str,
        id_field: str,
        key: str,
    ) -> str | None:
        """FreshBooks' stand-in for an idempotency key — see the module
        docstring for why this is weaker than QuickBooks' `requestid`."""
        payload = await self._request(
            "GET",
            f"{_API_ROOT}/{account_id}/{path}",
            access_token=access_token,
            params={"search[notes_like]": _tag(key)},
            what=path,
        )
        rows = payload.get("response", {}).get("result", {}).get(collection) or []
        for row in rows:
            if _tag(key) in (row.get("notes") or ""):
                return str(row[id_field])
        return None

    async def _search(
        self, *, access_token: str, account_id: str, path: str, collection: str, match
    ) -> dict[str, Any] | None:
        payload = await self._request(
            "GET",
            f"{_API_ROOT}/{account_id}/{path}",
            access_token=access_token,
            what=path,
        )
        rows = payload.get("response", {}).get("result", {}).get(collection) or []
        for row in rows:
            if match(row):
                return row
        return None

    async def _create(
        self, *, access_token: str, account_id: str, path: str, body: dict, collection: str
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            f"{_API_ROOT}/{account_id}/{path}",
            access_token=access_token,
            json=body,
            what=path,
        )
        result = payload.get("response", {}).get("result", {}).get(collection)
        if not result:
            raise AccountingProviderError(
                f"FreshBooks POST {path} returned no {collection} in its response"
            )
        return result

    async def _request(
        self,
        method: str,
        url: str,
        *,
        access_token: str,
        json: dict | None = None,
        params: dict[str, str] | None = None,
        what: str,
    ) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=60.0) as http:
            response = await http.request(
                method,
                url,
                json=json,
                params=params,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )

        if response.status_code in (401, 403):
            raise AccountingAuthError(
                f"FreshBooks rejected the access token ({response.status_code})"
            )
        if response.status_code >= 400:
            raise AccountingProviderError(
                f"FreshBooks {method} {what} failed ({response.status_code}): "
                f"{response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AccountingProviderError(
                f"FreshBooks {method} {what} returned a non-JSON body"
            ) from exc


def _tag(key: str, prefix: str | None = None) -> str:
    tag = f"{_KEY_PREFIX}{key}"
    return f"{prefix} [{tag}]" if prefix else tag
