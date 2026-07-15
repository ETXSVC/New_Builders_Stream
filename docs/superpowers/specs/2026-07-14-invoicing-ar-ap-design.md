# Invoicing / AR-AP / Profitability Reporting — Design Spec

**Implementation Status (2026-07-15): Complete.** All 20 tasks in [`docs/superpowers/plans/2026-07-14-invoicing-ar-ap.md`](../plans/2026-07-14-invoicing-ar-ap.md) are implemented and merged into `feature/invoicing-expenses`. Evidence: full backend suite (`pytest -v` from `backend/`) passed twice solo, 665/665, no failures (1303.54s and 1317.60s runs); the dedicated RLS regression suite (`test_tenant_isolation.py` + `test_rls_policy_regression.py` + `test_tenant_isolation_phase1.py` + `test_tenant_isolation_phase2.py` + `test_tenant_isolation_phase3.py` + `test_billing_tenant_isolation.py` + `test_invoicing_ap_tenant_isolation.py`) passed 68/68, confirming no regression in any Phase 0/1/2/Compliance Tracking/Billing table from this work; `scripts/e2e_smoke_test.py` was extended with this feature's own exit criterion (deposit-invoice auto-generation on Estimate approval, invoice send/payment/paid-transition, a vendor Bill with payment, and the profitability report reflecting both) and verified passing against a live Docker Compose stack rebuilt from this branch. The same pre-existing, out-of-scope infrastructure issue flagged during Billing's own live-stack verification (the frontend container's host port mapping and the smoke test's own `FRONTEND_URL` remain out of sync) was encountered again and deliberately NOT fixed here, per that task's own precedent — it is unrelated to this feature's correctness. Two real bugs in the plan's own draft code were found and fixed during implementation, not left for a future pass: an unquantized-Decimal rounding bug (Task 3.39's deposit-amount calculation, and again independently in Task 3.46's tax-liability estimate) inconsistent with this codebase's established `CENTS`/`ROUND_HALF_UP` convention, and a date-boundary bug in the profitability report (Task 3.46) where comparing a `DateTime` column directly against a bare `end_date` silently excluded same-day-created rows — both confirmed empirically before being fixed, not assumed.

**Date:** 2026-07-14
**Depends on:** Phase 0 (foundation), Phase 1 (CRM & Project Management), Phase 2 (Estimation Engine + E-Signature), Compliance Tracking, Billing — all merged to `main`.
**Scope source:** [`docs/01-prd.md`](../../01-prd.md) Section 6/8, [`docs/02-functional-requirements.md`](../../02-functional-requirements.md) Section 5 (US-5.2–US-5.8), [`docs/04-database-schema.md`](../../04-database-schema.md) Section 7, [`docs/05-api-specification.md`](../../05-api-specification.md) Section 6, [`docs/07-security-compliance.md`](../../07-security-compliance.md) Sections 2, 5, [`docs/09-roadmap-implementation-plan.md`](../../09-roadmap-implementation-plan.md) Phase 3.

## Decomposition note

Per the same explicit user decision that split Phase 3 into Compliance Tracking, Billing, and this spec, this is the third and final piece: **client-facing Accounts Receivable (Invoices), Accounts Payable (Bills), Expenses, and company-wide profitability reporting.** Builders Stream's own Stripe subscription billing (what the contractor pays to use the platform) is a separate, already-shipped concern (see `2026-07-13-billing-design.md`) and is not touched here — the existing business rule that these two financial flows "must not be conflated in the data model" still holds.

This spec started scoped to AR only (US-5.2–5.4 as originally written) and was expanded mid-design, on explicit direction, into a complete AR/AP integration once a gap analysis against standard accounting practice surfaced that AR-only would ship without invoice numbering, a void/cancellation path, or any accounts-payable tracking at all — a real product gap, not just a nice-to-have. `docs/02-functional-requirements.md` Section 5 now reflects the expanded scope (US-5.5–5.8).

## Scope

**Backend-only**, matching every prior phase. Explicitly **excluded**, each a deliberate scope cut:

- **QuickBooks/FreshBooks sync** — Phase 4, a separate future spec. This system is the source of record that syncs *to* real accounting software later, not a replacement for one.
- **Real payment gateway / client-facing checkout** — the Accountant *records* that a payment was received or made (e.g., a check arrived, a wire went out); no card processing, no Stripe integration for client billing.
- **Jurisdiction-aware tax calculation** — a single flat company-wide rate estimates liability; no per-state/per-item tax rules.
- **Expense/Bill approval workflow** — Admin/Accountant record directly, no submit-for-approval state machine (matches the existing RBAC matrix's "Full CRUD" grant, no PM/Field Crew involvement).
- **Double-entry bookkeeping / general ledger** — no chart of accounts, no debits/credits. This is a lightweight AR/AP ledger, not a GL.
- **Overpayment/credit-note handling** — if cumulative payments exceed an invoice/bill amount, `outstanding_balance` simply goes negative; no formal credit-memo entity. A real-world overpayment is handled by the Accountant outside this system for now.

**Included**: `invoices` (AR, with sequential numbering and a void path), `invoice_payments`, `bills` (AP, tied to an existing Subcontractor or a free-text vendor), `bill_payments`, `expenses` (unchanged from the original pre-sketch), automatic draft-invoice generation on Estimate approval, a unified overdue-flagging scheduled job, and a company-wide profitability report with AR/AP aging and estimated tax liability.

## 1. Data Model

```sql
CREATE TABLE invoices ( -- client-facing project invoices (AR)
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id),
    company_id UUID NOT NULL REFERENCES companies(id),
    estimate_id UUID REFERENCES estimates(id), -- NULL for invoices created directly, not auto-generated
    invoice_number VARCHAR(20) NOT NULL, -- per-company sequential, assigned at creation (e.g. INV-2026-0001) — unique PER COMPANY, not globally (see UNIQUE(company_id, invoice_number) below)
    amount NUMERIC(12,2) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','paid','overdue','void')),
    due_date DATE,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (company_id, invoice_number)
);

CREATE TABLE invoice_payments ( -- append-only ledger of payments RECEIVED from the client
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id UUID NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    company_id UUID NOT NULL REFERENCES companies(id),
    amount NUMERIC(12,2) NOT NULL,
    paid_date DATE NOT NULL,
    recorded_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE bills ( -- amounts owed to vendors/subcontractors (AP)
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id),
    project_id UUID REFERENCES projects(id), -- NULL for company-wide overhead bills
    subcontractor_id UUID REFERENCES subcontractors(id), -- NULL for non-Subcontractor vendors
    vendor_name VARCHAR(255), -- required when subcontractor_id is NULL
    bill_number VARCHAR(50), -- the vendor's own reference number, free text
    amount NUMERIC(12,2) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'unpaid' CHECK (status IN ('unpaid','paid','overdue','void')),
    due_date DATE,
    created_at TIMESTAMPTZ DEFAULT now(),
    CHECK (subcontractor_id IS NOT NULL OR vendor_name IS NOT NULL)
);

CREATE TABLE bill_payments ( -- append-only ledger of payments MADE to vendors
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id UUID NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    company_id UUID NOT NULL REFERENCES companies(id),
    amount NUMERIC(12,2) NOT NULL,
    paid_date DATE NOT NULL,
    recorded_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE expenses ( -- non-vendor project costs (petty cash, mileage, direct purchases)
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id),
    company_id UUID NOT NULL REFERENCES companies(id),
    description VARCHAR(255) NOT NULL,
    amount NUMERIC(12,2) NOT NULL,
    incurred_on DATE NOT NULL
);
```

Every table denormalizes `company_id` directly (own column, own FK) rather than relying on a join through a parent for RLS — same convention `estimate_line_items` already establishes for child tables. Standard `tenant_isolation` RLS policy (downward-visibility) on all five; none of this needs Billing's non-standard upward policy (unlike `subscriptions`, none of these tables are root-only).

`bills`' `CHECK (subcontractor_id IS NOT NULL OR vendor_name IS NOT NULL)` is expressible as a plain constraint (no cross-table lookup needed), unlike `subscriptions`' root-only ownership rule which needed application-layer enforcement.

**Reconciling `expenses` vs. `bills`**: `expenses` is the pre-existing, unchanged "no vendor, no payment lifecycle" cost category. `bills` is new — a specific vendor's payable obligation, with its own payment ledger. A project's actual cost (Section 6) sums both; nothing double-counts as long as a given cost is recorded as *either* an Expense *or* a Bill, never both — an application-layer discipline, not a DB constraint (there is no automatic way to prevent an Accountant from recording the same cost twice under both, matching this codebase's general trust-the-Accountant-role posture elsewhere).

**Module-level constants** (new `app/services/invoicing.py`, mirroring `app/services/billing.py`'s `TIER_INCLUDED_SEATS` pattern): `DEFAULT_DEPOSIT_PERCENTAGE` and `DEFAULT_TAX_RATE` — explicit placeholders, not validated business decisions, easily changed later without a migration.

## 2. Deposit-Invoice Auto-Generation (US-5.2)

New `app/services/estimate_approved_handler.py`, `handle_estimate_approved`, registered in `app/core/event_handlers.py` alongside `handle_lead_won` (same `is_registered()` re-registration guard — the module-global handler registry is cleared before/after every test by `tests/conftest.py`'s autouse fixture, so both real startup and each relevant test must call `register_event_handlers()` themselves).

Reuses the caller's `session` (never commits/rolls back itself — `get_current_user` owns the one commit per request, per Inherited Invariant #4), writes an `audit_log` entry (`action="invoice.auto_generated"`).

`ESTIMATE_APPROVED`'s own payload docstring (`app/routers/estimates.py`) already documents that `project_id` **may be `None`** (an Estimate approved against a bare Lead with no Project yet) and explicitly defers handling that to this spec. Since `invoices.project_id` is `NOT NULL`, this handler **no-ops silently when `project_id is None`** — no invoice, no error. If that Estimate's Project gets drafted later, there's no retroactive invoice generation (out of scope — would need its own trigger on project-creation, not estimate-approval).

When `project_id` is present: creates `Invoice(project_id=…, company_id=…, estimate_id=estimate_id, invoice_number=<next sequential>, amount=approved_total * DEFAULT_DEPOSIT_PERCENTAGE, status="draft", due_date=None)`. `due_date` stays `None` until `POST /invoices/{id}/send` (Section 3) — a draft invoice isn't "due" until an Accountant reviews and sends it.

**Invoice numbering**: a single, never-resetting per-company sequence, formatted `INV-{creation_year}-{counter}` (e.g. `INV-2026-0001`, then `INV-2026-0002`, ..., then `INV-2027-0047` the following year — the year is cosmetic, showing when that invoice was created, not an annual reset boundary; the counter itself keeps climbing). Assigned at creation via a guarded `SELECT COUNT(*) + 1 ... FOR UPDATE`-style locked increment scoped to `company_id` (counting all of that company's invoices ever, not filtered by year) — this codebase has no existing per-tenant sequence to reuse, so a straightforward locked-count approach is used rather than introducing new DB sequence infrastructure for one column.

## 3. AR Endpoints & Invoice Lifecycle (US-5.2, 5.6, 5.7, 5.8)

| Route | Method | RBAC | Behavior |
|---|---|---|---|
| `/projects/{id}/invoices` | POST | Admin, Accountant | Create a manual invoice — `amount` required, `due_date` optional. Status starts `draft`. |
| `/projects/{id}/invoices` | GET | Admin, Accountant, Client | List a project's invoices. Client sees non-`draft` only (same `if current.role == "client": query = query.where(...)` shape `list_estimates` already uses). |
| `/invoices/{id}` | GET | Admin, Accountant, Client | Detail, including its payments and a computed `outstanding_balance` (`amount - SUM(payments.amount)`). Same Client scoping. |
| `/invoices/{id}/send` | POST | Admin, Accountant | `draft → sent`. Body: `due_date` (required if not already set at creation; optional override otherwise). 409 if not currently `draft`. |
| `/invoices/{id}/payments` | POST | Admin, Accountant | Records a payment (`amount`, `paid_date`) — append-only. Cumulative payments reaching `amount` auto-flips status to `paid` in the same request. 409 if invoice is `draft` or `void`. |
| `/invoices/{id}/void` | POST | Admin, Accountant | Any non-`paid` status → `void`. Terminal — no further transitions. 409 if already `paid` or `void`. |

No `PATCH`/`DELETE` — matches the append-only discipline (void + reissue, never edit-in-place).

## 4. AP Endpoints & Bill Lifecycle (US-5.5, 5.7, 5.8)

Bills have an *optional* `project_id`, so unlike Invoices they get top-level routes rather than project-nested ones:

| Route | Method | RBAC | Behavior |
|---|---|---|---|
| `/bills` | POST | Admin, Accountant | Create a Bill (`project_id` optional, `subcontractor_id` optional, `vendor_name` required if no subcontractor, `amount`, `due_date` optional, `bill_number` optional). Status starts `unpaid`. |
| `/bills` | GET | Admin, Accountant | List, optionally filtered by `?project_id=` (omitted = company-wide, including overhead bills). |
| `/bills/{id}` | GET | Admin, Accountant | Detail, payments, computed `outstanding_balance`. |
| `/bills/{id}/payments` | POST | Admin, Accountant | Records a payment made to the vendor. Cumulative reaching `amount` auto-flips to `paid`, same mechanism as Invoice payments. |
| `/bills/{id}/void` | POST | Admin, Accountant | Any non-`paid` status → `void`. |

No Client access to any AP route — Bills are the company's own internal obligations, never client-facing. No `draft`/`sent` states — a Bill is an obligation the vendor already sent *to* the company (no approval workflow, per explicit scope decision), so it's payable the moment it's recorded.

## 5. Overdue Scheduled Job (US-5.8)

New `app/tasks/flag_overdue_financial_records.py`, mirroring `app/tasks/compliance_expiry.py`/`app/tasks/seat_usage.py`'s shape (undecorated `_flag_overdue_financial_records(session_factory)` for testability, decorated `flag_overdue_financial_records = dramatiq.actor(...)`), wired into `scheduler.py` as a new daily `add_job(...)`. One task covers both tables in the same owner-role transaction (`settings.migrations_database_url` — legitimately cross-tenant, scans every company):

```sql
UPDATE invoices SET status = 'overdue' WHERE status = 'sent' AND due_date < CURRENT_DATE;
UPDATE bills SET status = 'overdue' WHERE status = 'unpaid' AND due_date < CURRENT_DATE;
```

`paid`/`void` rows are untouched (excluded by the `WHERE` clause's status set) — a settled or voided record never gets flagged overdue regardless of `due_date`. No per-row `try/except: continue` like `seat_usage.py` needs — these are single set-based `UPDATE`s, not per-row calls to an external service that can fail mid-batch.

## 6. Expenses (US-5.3)

Unchanged from the original pre-sketch — `POST /projects/{id}/expenses` (Admin, Accountant), `GET /projects/{id}/expenses` (Admin, Accountant). No lifecycle, no vendor link. Feeds directly into Section 7's `actual_cost` calculation alongside `bills`.

## 7. Profitability Report — `GET /reports/profitability` (US-5.4)

**Per-project figures**, within the requested date range (filtered on `invoices.created_at`/`bills.created_at`):

- `billed_revenue` = `SUM(invoices.amount)` where status ∈ (`sent`,`paid`,`overdue`) — excludes `draft`/`void`. Deliberately **billed**, not the full Estimate total — a project with only a 10% deposit invoiced shows that 10% as recognized revenue, matching accrual-basis practice rather than overstating profitability on unbilled work.
- `actual_cost` = `SUM(expenses.amount) + SUM(bills.amount)` where bill status ≠ `void`.
- `profitability` = `billed_revenue - actual_cost`.

**Company-wide, point-in-time** (not date-range filtered — aging is inherently "as of now"):

- `ar_aging`: every non-`draft`/non-`void`/non-fully-paid invoice, with `outstanding_balance` and an aging bucket (`current`, `1-30`, `31-60`, `61-90`, `90+` days past `due_date`) — standard AR aging report shape.
- `ap_aging`: identical bucketing for outstanding Bills.
- `tax_liability_estimate` = `DEFAULT_TAX_RATE × SUM(billed_revenue across all projects, within the date range)`.

**Indexes**: `(company_id, status)` on both `invoices` and `bills` — the overdue job's own `WHERE` clause plus every aging query filters on exactly this pair, same "index what the actual queries filter on" convention `compliance_expiry`'s own table established.

## 8. RBAC & Read-Only Enforcement

The existing RBAC matrix's "Accounting/Billing" row is split into AR and AP (`docs/07-security-compliance.md` Section 2, already updated): both give Admin/Accountant Full CRUD and PM/Field Crew none; Client gets AR read-only (non-draft) and **zero** AP access.

Every write route above (Invoice create/send/payments/void, Bill create/payments/void, Expenses) gets `block_if_read_only` from day one — Billing's dependency already exists; this is a new module's routes launching with it wired in, not a later retrofit.

## 9. Testing Strategy

- **Tenant-isolation regression suite**: new `test_invoicing_ap_tenant_isolation.py`, following `test_billing_tenant_isolation.py`/`test_tenant_isolation_phase3.py`'s exact shape (header-spoofing rejection, sibling-branch blocking, parent-sees-child-branch).
- **Event-consumer test**: `ESTIMATE_APPROVED` → draft invoice, mirroring `test_lead_won_drafts_project.py`, including the documented no-op-on-`None`-`project_id` case.
- **Lifecycle tests**: `draft → sent → paid` and `unpaid → paid`, cumulative-payment-triggers-auto-paid (exact-boundary and overpayment-goes-negative cases), void blocked once `paid`, invoice-numbering uniqueness/sequential-per-company under concurrent creation.
- **Overdue job**: tested directly via its undecorated function, mirroring `compliance_expiry`/`seat_usage`.
- **Profitability report**: fixture-driven tests asserting exact `billed_revenue`/`actual_cost`/aging-bucket math against known seed data.
- **Standard RBAC tests** per endpoint, including the Client AR-only/zero-AP split.
- **Read-only enforcement**: the existing completeness introspection test (`test_read_only_enforcement.py`) already walks the live route table — no new test needed, it automatically catches any new route missing the dependency.
- **Full-Stack E2E Extension + Full Regression Pass + Plan Closeout**, same two closing tasks every prior phase has ended on.

## Open Questions Deferred, Not Silently Dropped

- Exact `DEFAULT_DEPOSIT_PERCENTAGE` and `DEFAULT_TAX_RATE` values are placeholders pending a real business decision, same status as Billing's per-tier seat defaults.
- Overpayment/credit-note handling, jurisdiction-aware tax, expense/bill approval workflows, and QuickBooks/FreshBooks sync are each explicitly out of scope (see Scope section) and remain open follow-up work.
