"""RealQuickBooksClient — the AccountingProviderClient that writes into a
tenant's actual QuickBooks Online company file.

Selected per-provider by `QUICKBOOKS_CLIENT_ID` (see
`app.services.accounting_client.get_accounting_client`); the fake stays the
default and remains what the test suite exercises end to end.

Plain REST over httpx rather than `python-quickbooks` or `intuit-oauth`:
this needs six endpoints, all documented and stable, and the community SDKs
are synchronous — wrapping a blocking HTTP client in a Dramatiq async actor
would block the worker's event loop for the duration of every call.

Three QuickBooks-specific facts drive most of the shape of this file:

* **`realmId` is not in the token response.** It arrives as a query
  parameter on the OAuth callback and is a path segment of every subsequent
  request. `exchange_code_for_tokens` therefore takes it from the caller
  (`callback_account_id`) rather than parsing it out of anything, and the
  callback route is what has to forward it. A token without its realm
  addresses nothing.

* **Idempotency is a `requestid` query parameter**, not a header. Passing
  the same value replays the original response instead of creating a second
  transaction — which is exactly what the sync actor needs, because
  Dramatiq is at-least-once and a push that succeeds before its own
  bookkeeping commit fails will be retried with the same entity_id.

* **Nothing can be created against a bare name.** An invoice needs a
  `CustomerRef` and a line needs an `ItemRef`; a bill needs a `VendorRef`
  and an expense account; a purchase needs a payment account. Each is a
  real id in the tenant's own chart of accounts, which is what
  `resolve_entity` exists to find (or create) and what the sync actor
  caches in `integration_entity_mappings`.
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

# The single service item every invoice line books against. QuickBooks
# requires an ItemRef on a SalesItemLineDetail and Builders Stream has no
# product catalogue to map from — estimates carry line items, but an
# invoice is one amount against a project. One well-named service item
# keeps the tenant's books readable; splitting invoices into real item
# lines is a bigger feature than an accounting sync.
_SERVICE_ITEM_NAME = "Construction Services"

# What each record type cannot be created without. `account` kinds pass an
# AccountType as the name, not a display name — see resolve_entity.
_REQUIRED_REFS: dict[str, tuple[RefSpec, ...]] = {
    "invoice": (
        RefSpec(payload_key="customer_id", kind="customer", from_payload="customer_name"),
        RefSpec(payload_key="item_id", kind="item", literal=_SERVICE_ITEM_NAME),
    ),
    "bill": (
        RefSpec(payload_key="vendor_id", kind="vendor", from_payload="vendor_name"),
        RefSpec(payload_key="expense_account_id", kind="account", literal="Expense"),
    ),
    "expense": (
        RefSpec(payload_key="expense_account_id", kind="account", literal="Expense"),
        RefSpec(payload_key="payment_account_id", kind="account", literal="Bank"),
    ),
}

_AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_SCOPE = "com.intuit.quickbooks.accounting"

_API_BASE = {
    "production": "https://quickbooks.api.intuit.com/v3/company",
    "sandbox": "https://sandbox-quickbooks.api.intuit.com/v3/company",
}

# What `kind` means to QuickBooks. The sync actor speaks in these kinds and
# never in QuickBooks nouns, so FreshBooks can answer the same questions.
_QUERY_BY_KIND = {
    "customer": ("Customer", "DisplayName"),
    "vendor": ("Vendor", "DisplayName"),
    "item": ("Item", "Name"),
}


def _escape(value: str) -> str:
    """Escape a value for QuickBooks' SQL-ish query language.

    A client named "O'Brien Construction" is not an edge case in this
    industry, and an unescaped apostrophe here is both a broken query and
    the shape of a query-injection bug.
    """
    return value.replace("\\", "\\\\").replace("'", r"\'")


@dataclass
class RealQuickBooksClient:
    client_id: str
    client_secret: str
    redirect_uri: str
    environment: str = "production"

    def __post_init__(self) -> None:
        if not self.client_secret:
            raise AccountingConfigurationError(
                "QUICKBOOKS_CLIENT_ID is set, so the real QuickBooks client is in use, "
                "but QUICKBOOKS_CLIENT_SECRET is not set"
            )
        if self.environment not in _API_BASE:
            raise AccountingConfigurationError(
                f"QUICKBOOKS_ENVIRONMENT must be one of {sorted(_API_BASE)}, "
                f"got {self.environment!r}"
            )

    def required_refs(self, record_type: str) -> tuple[RefSpec, ...]:
        return _REQUIRED_REFS.get(record_type, ())

    # --- OAuth -----------------------------------------------------------

    async def get_authorization_url(self, *, company_id: str, state: str) -> str:
        from urllib.parse import urlencode

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": _SCOPE,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code_for_tokens(
        self, *, code: str, callback_account_id: str | None = None
    ) -> AccountingTokens:
        if not callback_account_id:
            # Failing here beats storing a token that cannot address
            # anything and discovering it at the first sync, hours later,
            # as an opaque 401.
            raise AccountingProviderError(
                "QuickBooks did not return a realmId on the callback; cannot address "
                "a company file without it"
            )
        payload = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        return AccountingTokens(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            account_id=callback_account_id,
        )

    async def refresh_access_token(self, *, refresh_token: str) -> AccountingTokens:
        payload = await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )
        # QuickBooks rotates: the response carries a NEW refresh token and
        # the one just used stops working. Returning it is only half the
        # job — the caller must persist the pair, which is why this returns
        # AccountingTokens rather than just an access token.
        return AccountingTokens(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
        )

    async def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        import base64

        import httpx

        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.post(
                _TOKEN_URL,
                data=data,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        if response.status_code in (400, 401, 403):
            # A refresh that fails this way is terminal for the connection:
            # the refresh token is expired (100 days) or already rotated
            # away. AccountingAuthError rather than a generic error so the
            # actor does not treat it as "refresh and try again", which is
            # exactly what just failed.
            raise AccountingAuthError(
                f"QuickBooks token request rejected ({response.status_code}): {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise AccountingProviderError(
                f"QuickBooks token request failed ({response.status_code}): {response.text[:200]}"
            )
        return response.json()

    # --- Entity resolution ------------------------------------------------

    async def resolve_entity(
        self, *, access_token: str, account_id: str, kind: str, name: str
    ) -> str:
        if kind in _QUERY_BY_KIND:
            entity, name_field = _QUERY_BY_KIND[kind]
            found = await self._query_one(
                access_token=access_token,
                account_id=account_id,
                statement=(
                    f"select Id from {entity} where {name_field} = '{_escape(name)}'"
                ),
                entity=entity,
            )
            if found is not None:
                return str(found["Id"])
            return await self._create_entity(
                access_token=access_token, account_id=account_id, kind=kind, name=name
            )

        if kind == "account":
            # `name` is an AccountType here ('Expense', 'Bank', 'Income'),
            # not a display name — the sync actor asks for a ROLE and takes
            # whichever account fills it. Deliberately never creates one:
            # inventing accounts in somebody's chart of accounts is a
            # bookkeeping decision, not an integration's to make.
            found = await self._query_one(
                access_token=access_token,
                account_id=account_id,
                statement=(
                    f"select Id from Account where AccountType = '{_escape(name)}' "
                    "and Active = true maxresults 1"
                ),
                entity="Account",
            )
            if found is None:
                raise AccountingProviderError(
                    f"No active QuickBooks account of type {name!r} exists in this company "
                    "file; one is required to post this record"
                )
            return str(found["Id"])

        raise AccountingProviderError(f"Unknown entity kind for QuickBooks: {kind!r}")

    async def _create_entity(
        self, *, access_token: str, account_id: str, kind: str, name: str
    ) -> str:
        if kind == "customer":
            body: dict[str, Any] = {"DisplayName": name}
            resource = "customer"
            key = "Customer"
        elif kind == "vendor":
            body = {"DisplayName": name}
            resource = "vendor"
            key = "Vendor"
        elif kind == "item":
            # A Service item cannot be created without an income account to
            # book its revenue against, so this resolves one first. That is
            # a lookup, never a create — see the 'account' branch above.
            income_account_id = await self.resolve_entity(
                access_token=access_token, account_id=account_id, kind="account", name="Income"
            )
            body = {
                "Name": name,
                "Type": "Service",
                "IncomeAccountRef": {"value": income_account_id},
            }
            resource = "item"
            key = "Item"
        else:  # pragma: no cover - guarded by resolve_entity above
            raise AccountingProviderError(f"Cannot create QuickBooks entity of kind {kind!r}")

        payload = await self._api(
            "POST",
            access_token=access_token,
            account_id=account_id,
            resource=resource,
            json=body,
        )
        return str(payload[key]["Id"])

    # --- Pushes -----------------------------------------------------------

    async def push_invoice(
        self, *, access_token: str, account_id: str, invoice: dict, idempotency_key: str
    ) -> str:
        line: dict[str, Any] = {
            "Amount": float(invoice["amount"]),
            "DetailType": "SalesItemLineDetail",
            "SalesItemLineDetail": {"ItemRef": {"value": invoice["item_id"]}},
        }
        if invoice.get("description"):
            line["Description"] = invoice["description"]

        body: dict[str, Any] = {
            "CustomerRef": {"value": invoice["customer_id"]},
            "Line": [line],
        }
        if invoice.get("invoice_number"):
            # The tenant's bookkeeper reconciles on this number, so it is
            # worth carrying across. QuickBooks requires DocNumber to be
            # unique within the company file when duplicate-warning is on,
            # so a number that collides with a pre-existing QuickBooks
            # invoice fails this push — visibly, in sync-status, with the
            # provider's own message, rather than silently posting under a
            # different number nobody can reconcile against.
            body["DocNumber"] = invoice["invoice_number"]
        if invoice.get("due_date"):
            body["DueDate"] = invoice["due_date"]

        payload = await self._api(
            "POST",
            access_token=access_token,
            account_id=account_id,
            resource="invoice",
            json=body,
            request_id=idempotency_key,
        )
        return str(payload["Invoice"]["Id"])

    async def push_bill(
        self, *, access_token: str, account_id: str, bill: dict, idempotency_key: str
    ) -> str:
        body: dict[str, Any] = {
            "VendorRef": {"value": bill["vendor_id"]},
            "Line": [
                {
                    "Amount": float(bill["amount"]),
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {"value": bill["expense_account_id"]}
                    },
                }
            ],
        }
        if bill.get("bill_number"):
            body["DocNumber"] = bill["bill_number"]
        if bill.get("due_date"):
            body["DueDate"] = bill["due_date"]

        payload = await self._api(
            "POST",
            access_token=access_token,
            account_id=account_id,
            resource="bill",
            json=body,
            request_id=idempotency_key,
        )
        return str(payload["Bill"]["Id"])

    async def push_expense(
        self, *, access_token: str, account_id: str, expense: dict, idempotency_key: str
    ) -> str:
        # QuickBooks models a recorded expense as a Purchase, not an
        # "Expense" resource — the noun does not exist in the v3 API.
        line: dict[str, Any] = {
            "Amount": float(expense["amount"]),
            "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": expense["expense_account_id"]}
            },
        }
        if expense.get("description"):
            line["Description"] = expense["description"]

        body: dict[str, Any] = {
            # Cash is the least wrong default: it asserts only that money
            # left, which is all a Builders Stream expense records. There
            # is no payment-method field on `expenses` to map from, so
            # inferring anything narrower would be inventing data.
            "PaymentType": "Cash",
            "AccountRef": {"value": expense["payment_account_id"]},
            "Line": [line],
        }
        payload = await self._api(
            "POST",
            access_token=access_token,
            account_id=account_id,
            resource="purchase",
            json=body,
            request_id=idempotency_key,
        )
        return str(payload["Purchase"]["Id"])

    # --- HTTP -------------------------------------------------------------

    async def _query_one(
        self, *, access_token: str, account_id: str, statement: str, entity: str
    ) -> dict[str, Any] | None:
        payload = await self._api(
            "GET",
            access_token=access_token,
            account_id=account_id,
            resource="query",
            params={"query": statement},
        )
        rows = payload.get("QueryResponse", {}).get(entity, [])
        return rows[0] if rows else None

    async def _api(
        self,
        method: str,
        *,
        access_token: str,
        account_id: str,
        resource: str,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        import httpx

        url = f"{_API_BASE[self.environment]}/{account_id}/{resource}"
        query = dict(params or {})
        if request_id is not None:
            # THE idempotency mechanism. Same value replays the original
            # response rather than creating a second transaction, which is
            # what makes Dramatiq's at-least-once delivery safe here.
            query["requestid"] = request_id

        async with httpx.AsyncClient(timeout=60.0) as http:
            response = await http.request(
                method,
                url,
                params=query or None,
                json=json,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )

        if response.status_code in (401, 403):
            # The one error the sync actor retries differently: refresh the
            # access token and try once more.
            raise AccountingAuthError(
                f"QuickBooks rejected the access token ({response.status_code})"
            )
        if response.status_code >= 400:
            raise AccountingProviderError(
                f"QuickBooks {method} {resource} failed ({response.status_code}): "
                f"{response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise AccountingProviderError(
                f"QuickBooks {method} {resource} returned a non-JSON body"
            ) from exc
