# External Integrations (QuickBooks / FreshBooks) — Provider-Agnostic Core Design Spec

**Date:** 2026-07-15
**Depends on:** Phase 0 (foundation), Phase 1 (CRM & Project Management), Phase 2 (Estimation Engine + E-Signature), Compliance Tracking, Billing, Invoicing/AR-AP/Profitability Reporting — all merged to `main`.
**Scope source:** [`docs/01-prd.md`](../../01-prd.md) Section 6, [`docs/02-functional-requirements.md`](../../02-functional-requirements.md) Section 6 (US-6.1–6.2), [`docs/03-technical-architecture.md`](../../03-technical-architecture.md) Section 4, [`docs/04-database-schema.md`](../../04-database-schema.md) Section 7, [`docs/05-api-specification.md`](../../05-api-specification.md) Section 7, [`docs/07-security-compliance.md`](../../07-security-compliance.md) Sections 2, 4, [`docs/09-roadmap-implementation-plan.md`](../../09-roadmap-implementation-plan.md) Phase 4.

## Decomposition note

`docs/09-roadmap-implementation-plan.md`'s Phase 4 bundles QuickBooks and FreshBooks — two real, independent third-party integrations, each with its own OAuth flow, token shape, and sync API. Per the same explicit user decision that split Phase 3 into three separate specs (Compliance Tracking, Billing, Invoicing/AR-AP), this spec deliberately does **neither** provider yet. It builds the **provider-agnostic integrations core** — OAuth connect/callback plumbing, an `AccountingProviderClient` abstraction, event-driven sync triggers, retry, and per-record sync status — entirely behind a `FakeAccountingProviderClient`, the same "prove the whole pattern behind a fake, swap in the real SDK later with zero consumer changes" approach `docs/superpowers/specs/2026-07-13-billing-design.md` already used for Stripe. Wiring a real `RealQuickBooksClient` or `RealFreshBooksClient` against live OAuth credentials is explicit future work, one spec per provider, once this core is proven.

## Scope

**Backend-only**, matching every prior phase's precedent. Explicitly **excluded** (each a deliberate, discussed scope cut, not an oversight):

- **Real QuickBooks or FreshBooks SDK/API wiring.** This spec never makes a real HTTP call to either provider. `FakeAccountingProviderClient` is the only implementation built and exercised in tests.
- **PROJECT_COMPLETED and INVOICE_PAID events.** `docs/03-technical-architecture.md`'s own event table lists both as eventual sync triggers, but neither is published anywhere in the codebase today (only `LEAD_WON` and `ESTIMATE_APPROVED` are real). This spec does not implement them — sync triggers on Invoice/Expense/Bill **creation** only, via three new events this spec does add (`INVOICE_CREATED`, `EXPENSE_CREATED`, `BILL_CREATED`). Syncing on later status changes (a paid Invoice, a completed Project) is future work once those events themselves exist.
- **Frontend UI** for the connect button, sync-status dashboard, or OAuth redirect handling — no frontend work exists for any phase in this codebase yet.

**Included**: the `integration_connections` and `integration_sync_records` tables, an `AccountingProviderClient` Protocol + `FakeAccountingProviderClient`, `GET /integrations/{provider}/connect` and `GET /integrations/{provider}/callback`, `GET /integrations/{provider}/sync-status`, three new events wired into the existing three `create_*` routes (Invoice, Expense, Bill), a generic Dramatiq sync actor with retry, and full tenant-isolation coverage.

## 1. Data Model

```sql
CREATE TABLE integration_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id),
    provider VARCHAR(20) NOT NULL CHECK (provider IN ('quickbooks','freshbooks')),
    access_token_encrypted TEXT NOT NULL,
    refresh_token_encrypted TEXT NOT NULL,
    connected_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (company_id, provider)
);

CREATE TABLE integration_sync_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id),
    connection_id UUID NOT NULL REFERENCES integration_connections(id),
    entity_type VARCHAR(20) NOT NULL CHECK (entity_type IN ('invoice','expense','bill')),
    entity_id UUID NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('pending','success','failed')) DEFAULT 'pending',
    attempt_count INT NOT NULL DEFAULT 0,
    last_error TEXT,
    last_attempted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (connection_id, entity_type, entity_id)
);
CREATE INDEX idx_integration_sync_records_connection_status ON integration_sync_records(connection_id, status);
```

`integration_connections.UNIQUE (company_id, provider)` — one active connection per company per provider, but a company can hold both a QuickBooks and a FreshBooks connection simultaneously (e.g. migrating between providers). Reconnecting the same provider (`callback` firing again) is an `ON CONFLICT (company_id, provider) DO UPDATE`, replacing stale tokens rather than erroring.

`integration_sync_records` is **mutable current-state, not an append-only log** — one row per `(connection_id, entity_type, entity_id)`, updated in place as attempts happen (`attempt_count` increments, `status`/`last_error` reflect the latest try). This was an explicit design choice: it matches how Dramatiq's own retry re-runs the *same* logical job rather than creating a new one, and it answers "does this Invoice show as synced right now," which is what a status indicator actually needs — not a full attempt history. If a company has zero active connections when an Invoice/Expense/Bill is created, no `integration_sync_records` row is ever created for that record — same "nothing to do" no-op shape `estimate_approved_handler.py` already uses when `project_id is None`.

Both tables get the standard `tenant_isolation` RLS policy (with `get_all_descendant_ids()` parent/child visibility), identical in shape to every other table in this codebase.

**New required setting**: `integration_token_encryption_key` (a Fernet key, `cryptography` package — genuinely new dependency, nothing in this codebase does token-at-rest encryption today). Required per `docs/07-security-compliance.md`'s existing statement that OAuth tokens "are additionally encrypted at the application layer before storage, never stored in plaintext." OAuth `state` signing reuses the existing `jwt_secret` under a distinct HMAC context (not a second new secret) — see Section 3.

## 2. `AccountingProviderClient` Protocol & Fake Implementation

Typed methods per operation, not one generic dispatch method — matching `StripeClient`'s own established shape (`create_customer`, `create_trialing_subscription`, `create_portal_session`, `report_seat_usage` are all separate methods), since a real QuickBooks/FreshBooks SDK genuinely has separate endpoints per record type and a real client implementation will need type-specific handling internally regardless.

```python
class AccountingProviderClient(Protocol):
    async def get_authorization_url(self, *, company_id: str, state: str) -> str: ...
    async def exchange_code_for_tokens(self, *, code: str) -> tuple[str, str]: ...  # (access_token, refresh_token)
    async def push_invoice(self, *, access_token: str, invoice: dict) -> str: ...  # returns external record id
    async def push_expense(self, *, access_token: str, expense: dict) -> str: ...
    async def push_bill(self, *, access_token: str, bill: dict) -> str: ...
```

`get_accounting_client(provider: str) -> AccountingProviderClient` is the one seam a real implementation plugs into later. Today it always returns `FakeAccountingProviderClient(provider=provider)` regardless of which provider string is passed — `provider` only shapes cosmetic details (the fake authorization URL's hostname, fake external-ID prefixes), never behavior, since there's nothing real to branch between yet. When `RealQuickBooksClient`/`RealFreshBooksClient` exist, this factory is the only place that changes.

`FakeAccountingProviderClient` follows `FakeStripeClient`'s own accumulator pattern — `pushed_invoices`/`pushed_expenses`/`pushed_bills: list[dict]` fields tests assert against — plus a `should_fail: bool` field that makes every `push_*` call raise `AccountingProviderError`, the one test hook needed to exercise the retry/failure path without real network flakiness.

## 3. OAuth Connect/Callback Flow

```
GET /integrations/{provider}/connect   — Admin/Accountant, requires CurrentUser
GET /integrations/{provider}/callback  — no CurrentUser (external redirect), ?code=...&state=...
```

Both are **GET**, matching OAuth 2.0's actual authorization-code-flow shape (RFC 6749 §4.1.2 — the authorization server redirects the user-agent back to the callback URL with `code`/`state` as query parameters, never a POST body) and the API spec's own existing sketch.

**`connect`** generates a signed, short-lived `state` token — an HMAC-signed payload (`company_id`, `provider`, a short expiry) using the existing `jwt_secret` under a distinct signing context, not a new secret or a database-backed nonce table. It calls `client.get_authorization_url(company_id=..., state=...)` and returns `{"authorization_url": "..."}` as a **JSON body, not a raw HTTP redirect** — deviating from the API spec's literal "(redirects)" annotation to match `POST /subscriptions/portal-session`'s own established precedent (hand back a URL, let the frontend redirect; there is no frontend in this codebase to actually receive a redirect target).

**`callback`** has **no `CurrentUser`** — the same structural reality `/invitations/{id}/accept` already established (an external redirect cannot carry a bearer token). The signed `state` query parameter *is* the authentication: `callback` verifies its signature and expiry, decodes `company_id` from it (400 on an invalid or expired state — never trust `company_id` from anywhere else in this request), then calls `client.exchange_code_for_tokens(code=...)`, encrypts both returned tokens with `integration_token_encryption_key`, and upserts the `integration_connections` row, writing an `integration.connected` audit log entry. `callback` joins the `block_if_read_only` exclusion list for the identical reason `/invitations/{id}/accept` is already excluded there: no `CurrentUser` exists to check a subscription's status against.

## 4. Event Bus Wiring & Sync Trigger (US-6.2)

US-6.2 requires syncing to be **asynchronous** — the event handler itself does no sync work, it only decides what needs syncing and enqueues it, the same "thin wrapper enqueues, worker does the real work" split `app/scheduler.py`'s own job wrappers already establish for scheduled jobs.

**Three new events**, published from **every place these entities are created** — the three `create_*` routes, plus `handle_estimate_approved` (`app/services/estimate_approved_handler.py`), which is the codebase's second Invoice-construction site: it auto-drafts a deposit Invoice on `ESTIMATE_APPROVED`, and that deposit invoice is arguably the flagship US-6.2 flow (client signs an Estimate → the deposit invoice lands in the accountant's platform), so it must publish `INVOICE_CREATED` too or auto-drafted invoices silently bypass sync while manually created ones work. (Found by external design review; any future Invoice/Bill/Expense creation path — recurring invoices, imports, retainage — inherits the same obligation.) Each payload carries its own `entity_type` explicitly, the same way `ESTIMATE_APPROVED`'s payload carries `approved_total` directly rather than making its handler re-derive it:

```python
await publish("INVOICE_CREATED", session=current.session, entity_type="invoice", entity_id=invoice.id, company_id=invoice.company_id)
await publish("EXPENSE_CREATED", session=current.session, entity_type="expense", entity_id=expense.id, company_id=expense.company_id)
await publish("BILL_CREATED",    session=current.session, entity_type="bill",    entity_id=bill.id,    company_id=bill.company_id)
```

**One handler function**, `handle_financial_record_created(*, session, entity_type, entity_id, company_id, **_ignored)`, registered against all three events (same `app.core.events.publish()` mechanics as every other handler — `await handler(**payload)`, so `entity_type` arrives as a normal keyword argument, no per-event wrapper needed). It queries `integration_connections WHERE company_id = :company_id` for every active connection and enqueues **one Dramatiq message per connection found**:

```python
sync_financial_record.send(connection_id=str(conn.id), entity_type=entity_type, entity_id=str(entity_id))
```

Zero connections → silent no-op. Enqueuing per-connection (not one message that internally loops over every connection) matters for retry correctness: if a QuickBooks push fails but a FreshBooks push for the same record succeeds, Dramatiq only retries the failed QuickBooks message — a successful FreshBooks sync is never redundantly redone.

## 5. Generic Sync Actor & Retry Semantics

`sync_financial_record` (`max_retries=3`, matching `flag_overdue_financial_records`'s own retry count) runs in the `worker` container with its own `AsyncSession` — the established PDF-export/compliance-expiry/overdue-flagging pattern, since a background job has no request-scoped session to inherit. Session-pattern disambiguation (the three cited precedents split into two different shapes): this actor uses the **owner-role engine** (`settings.migrations_database_url`, the `flag_overdue`/`compliance_expiry` shape, NOT `estimate_pdf.py`'s `SessionLocal` + `set_current_tenant` shape) **with explicit `connection_id`/`company_id` filters on every query** — the actor is per-tenant in intent (one `connection_id` per message) but there is no `set_current_tenant()` call in a worker process, and the payload carries only `connection_id`, so the initial connection lookup itself couldn't run under RLS without a chicken-and-egg. The explicit-filter discipline every existing worker actor already follows is the compensating control; this is the deliberate, implemented choice, not an oversight.

Per invocation: upsert the `integration_sync_records` row to `status='pending'` and increment `attempt_count`; fetch the connection and the actual Invoice/Expense/Bill row; decrypt the connection's access token; call the matching typed client method (`push_invoice`/`push_expense`/`push_bill`) with a serialized dict of the record's own fields; on success, mark the row `status='success'`; on any exception, mark the row `status='failed'` with `last_error=str(exc)` **then re-raise** — this codebase's convention is to let Dramatiq's own retry/backoff handle the actual retry timing (same as every other actor here), never a custom retry loop. After `max_retries` is exhausted, Dramatiq drops the message but the `integration_sync_records` row stays at `status='failed'`, remaining visible via the sync-status endpoint rather than silently vanishing.

## 6. Sync Status Endpoint

`GET /integrations/{provider}/sync-status` — Admin/Accountant only. 404s if the company has no `integration_connections` row for that provider. Otherwise returns `{provider, connected_at, records: [...]}`, where `records` is a cursor-paginated list of that connection's `integration_sync_records` rows (`entity_type`, `entity_id`, `status`, `attempt_count`, `last_error`, `last_attempted_at` — same `paginate()` helper every other list endpoint in this codebase already uses, since this table can grow to one row per financial record per connection and must never be returned unpaginated), with an optional `?status=failed` query filter so a monitoring view can specifically request the backlog rather than scanning every record.

## 7. RBAC & Tenant Isolation

| Module | Admin | Project Manager | Field Crew | Accountant | Client |
|---|---|---|---|---|---|
| Integrations | Full CRUD | — | — | Full CRUD | — |

Matches Billing's/AR/AP's own precedent exactly — no Client, Project Manager, or Field Crew access to any Integrations route. `callback` is the one route with no role check at all, because it has no `CurrentUser` to check (Section 3) — its security boundary is the signed `state` parameter, not `require_role`.

Both new tables get full RLS tenant-isolation regression coverage: header-spoofing rejection, an RLS-policy-level disable/re-enable proof, and parent/child branch visibility (including a genuinely bidirectional sibling-branch check) — the same structure `test_invoicing_ap_tenant_isolation.py` already established, in a new `test_integrations_tenant_isolation.py`.

## 8. Testing Strategy

- `FakeAccountingProviderClient` unit tests, including its `should_fail` hook.
- Router tests: `connect` returns an `authorization_url`; `callback` with a validly signed `state` creates the connection and an `integration.connected` audit entry; an invalid or expired `state` 400s; RBAC negatives (`project_manager`/`client` 403) on `connect` and `sync-status`.
- Event-wiring tests: creating an Invoice/Expense/Bill while a connection is active enqueues a sync message — intercepted via `monkeypatch` on `.send()`, the same technique `test_scheduler.py` already uses to avoid touching real Redis in an automated test. A company with zero connections enqueues nothing.
- Actor tests: call the undecorated `_sync_financial_record` directly against the real test DB (same shape `test_flag_overdue_financial_records.py` already establishes) — a successful push sets `status='success'`; a `should_fail=True` fake client sets `status='failed'` + `last_error` and re-raises; a second invocation increments `attempt_count`.
- Dedicated tenant-isolation regression file for both new tables (Section 7).

## Open Questions Deferred, Not Silently Dropped

- **Real `RealQuickBooksClient`/`RealFreshBooksClient` implementations** — explicit future work, one spec per provider, once real OAuth app credentials exist for each. The consumer surface (routes, handler, actor) is designed to survive that swap unchanged, but the follow-up spec must address two known contract gaps this design consciously leaves open (found by external design review — acknowledged here so they are inherited as requirements, not silent gaps):
  - **Push idempotency**: Dramatiq is at-least-once. If a real `push_*` succeeds at the provider but the follow-up `integration_sync_records` UPDATE/commit fails (worker SIGTERM, DB blip), the retry re-pushes and the customer's real QuickBooks gets a duplicate invoice. The real-provider spec must add a nullable `external_record_id` column to `integration_sync_records` (persist the returned id before the status flip; short-circuit or verify-instead-of-create on retry when it's already set) and/or an `idempotency_key` parameter on each `push_*` Protocol method (derivable as a stable hash of `connection_id:entity_type:entity_id`, forwarded as the provider's own Idempotency-Key header). Harmless today — the fake client's duplicates have no external effect — but this means the swap is NOT "only the factory changes": it includes one small migration and/or one Protocol-signature change.
  - **Token refresh**: the Protocol has no `refresh_access_token` method and the actor never reads `refresh_token_encrypted` back. Fine for the fake (its tokens never expire); fatal for real QuickBooks (1-hour access tokens) — every sync would 401 within an hour of connecting, retry with the same expired token, and permanently fail. The real-provider spec must add `refresh_access_token(*, refresh_token) -> tuple[str, str]` to the Protocol plus actor logic that, on an auth-specific error, refreshes, re-encrypts and persists the new pair back to `integration_connections` (the same `ON CONFLICT (company_id, provider) DO UPDATE` upsert path `callback` already uses), and retries the push once inline.
- **PROJECT_COMPLETED and INVOICE_PAID as sync triggers** — deferred until those events themselves are implemented (neither exists in the codebase today); syncing on Invoice/Bill *payment* or Project *completion*, not just creation, is a real gap this spec knowingly leaves for later.
- **Disconnecting a provider** (revoking a connection) has no route in this spec — only connect/callback/sync-status. A `DELETE /integrations/{provider}` is a small, obvious follow-up not included here because it wasn't in the original API spec sketch and doesn't block the Phase 4 exit criterion ("Enterprise-tier companies can connect and see a successful, monitored sync").
