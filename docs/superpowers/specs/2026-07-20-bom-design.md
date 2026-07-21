# Bill of Materials (BOM) — Design Spec

**Date:** 2026-07-20
**Depends on:** Estimation + E-Signature (PR #19, open — this spec assumes it merges first; BOM auto-generation is triggered from the estimate-approval event that sub-project introduced). Builds on the existing Estimation data model (`Estimate`, `EstimateLineItem`, `CostCatalogItem`) and reuses the BFF/route-handler and tab-navigation conventions established in CRM+PM and Estimation+E-Signature.
**Scope:** A standalone sub-project, not part of the original six-sub-project frontend sequence. Requested directly by the user as its own scoped feature.

## Goal

Give a PM a procurement/ordering list, generated automatically from a project's approved estimates, so they always know what materials are still needed, what's been ordered, and what's arrived — without manually re-deriving it from the estimate line items.

## Decision 1: Origin, cardinality, and scope

A BOM exists only for project-backed estimates (an estimate approved against a bare lead has no project yet, so nothing to generate against — mirrors the existing `ESTIMATE_APPROVED` draft-invoice handler's own `project_id is None` no-op). There is one BOM per project, not one per estimate: it is a flat collection of `BomLine` rows keyed to `project_id` directly, with no separate "Bom" header record to manage.

The BOM is add-only. Approving further estimates against the same project (e.g. a change-order estimate) adds to the existing BOM rather than replacing it. A PM can also add lines manually that don't come from any estimate (e.g. materials discovered mid-job) — these are marked with a `source` of `"manual"` rather than `"estimate"`.

BOM data stays self-contained: no automatic integration with AP/Bills/Expenses. A PM tracking "we ordered $4,000 of lumber" on a BOM line does not itself create a Bill or Expense record — that remains a manual, separate action in the Invoicing module, unchanged by this feature.

## Decision 2: Data model

Three new tables, all under the existing `"estimation"` tier module and the standard tenant-isolation RLS policy used by every other table in this domain:

**`Vendor`**
- `id`, `company_id`, `name`, `contact_email` (nullable), `contact_phone` (nullable), `notes` (nullable), `created_at`.
- Company-scoped exactly like `CostCatalogItem` — a simple supplier directory, not a full CRM entity. No linkage to Subcontractor (a distinct existing concept for compliance-tracked labor, not a materials supplier).

**`BomLine`**
- `id`, `project_id`, `cost_catalog_item_id` (nullable FK — null for manual lines), `description`, `unit`, `quantity` (numeric), `ordered` (bool, default false), `ordered_at` (nullable timestamp), `vendor_id` (nullable FK to `Vendor`), `source` (`"estimate"` | `"manual"`), `created_at`, `updated_at`.
- `description` and `unit` are copied from the `CostCatalogItem` at generation time (same snapshot approach `EstimateLineItem.unit_rate_snapshot` already uses elsewhere in this domain) so a later catalog edit doesn't retroactively change what a BOM line says. Manual lines set them directly from user input.
- Status is never stored as its own column — it's computed (see Decision 3).

**`BomLineReceipt`**
- `id`, `bom_line_id`, `quantity` (numeric), `received_at`, `recorded_by_user_id`.
- An append-only ledger of delivery events, not a single mutable "amount received so far" field. This mirrors the existing `InvoicePayment`/`BillPayment` pattern in this codebase (a running total is always the sum of discrete recorded events, preserving a real audit trail of when materials actually arrived and who logged it) rather than introducing a new "just overwrite the number" pattern this app doesn't otherwise use.

## Decision 3: Status derivation

A `BomLine`'s displayed status is computed from `ordered` and `SUM(BomLineReceipt.quantity for that line)` (call this `quantity_received`), never stored directly:

| Condition | Status |
|---|---|
| `ordered = false`, `quantity_received = 0` | **Needed** |
| `ordered = true`, `quantity_received = 0` | **Ordered** |
| `0 < quantity_received < quantity` | **Partially received** |
| `quantity_received >= quantity` | **Received** |

Marking a line "ordered" (setting `ordered = true`, `ordered_at = now`, optionally attaching a `vendor_id`) and recording a receipt (inserting a `BomLineReceipt`) are two independent actions — a line can accumulate receipts without ever having been explicitly marked "ordered" (e.g. a PM logging a delivery they didn't personally order), in which case status reads directly off the quantity-received condition.

## Decision 4: Auto-generation and merging on estimate approval

A new handler, `handle_estimate_approved_bom`, subscribed to the existing `ESTIMATE_APPROVED` event alongside the current draft-invoice handler (`app/services/estimate_approved_handler.py`'s `handle_estimate_approved`). It follows that handler's established shape exactly:

- Reuses the caller's session (`current.session` from `approve_estimate`'s own route handler) — never calls `session.commit()` or `session.rollback()` itself, only `flush()` (Inherited Invariant #4).
- No-ops silently when `project_id is None` (estimate approved against a bare lead).
- Gated by `tier_allows(session, company_id, "estimation")` before writing anything, matching the existing accounting-tier gate on the draft-invoice handler — without this, a company whose plan doesn't include Estimation could still get BOM lines auto-created through the event bus, bypassing the module boundary its routes already enforce.
- Writes an audit log entry (`action="bom_line.auto_generated"`) per created/updated line, `actor_id=None` for the same documented reason the existing handler uses (`ESTIMATE_APPROVED`'s publish payload carries no actor).

`EstimateLineItem.cost_catalog_item_id` is `nullable=False` (verified against `backend/app/models/estimate_line_item.py`) — every line item on an approved estimate always references a catalog item, so all of them participate: for each `EstimateLineItem`, look up an existing `BomLine` for `(project_id, cost_catalog_item_id)`. If one exists (a later estimate needs the same material), add this estimate's `quantity` to it. Otherwise create a new line with `source="estimate"`, `quantity` from the line item, `description`/`unit` snapshotted from the catalog item.

This means: the first approved estimate on a project creates its BOM; each subsequent approval (including change-order estimates) tops up existing lines or appends new ones. A PM never has to manually "generate" or "regenerate" a BOM.

## Decision 5: Routes and information architecture

New route group additions, following the existing tab-and-nav conventions:

| Route | Roles | Content |
|---|---|---|
| `/materials` | admin/PM | New top-level nav item. Every `BomLine` across the company's projects, with a status filter (needed/ordered/partially received/received) and a project filter. Each row: project name, description, quantity, quantity received, status, vendor. Status-edit controls inline (mark ordered + assign vendor, record a receipt) for admin/PM. This is the PM's daily "what am I still waiting on" cross-project view — read and status-edit only, no line creation here. |
| `/projects/[id]` → **Materials** tab | admin/PM (staff view only) | New tab alongside the existing Overview/Phases & tasks/Documents/Daily logs/Change orders/Estimates tabs. Same `BomLine` rows, scoped to this project, plus a manual "Add line" form (description, unit, quantity — no catalog item required) since this is where a PM is working in-context on a specific job. Client role does not get this tab — BOM/Materials is an internal procurement tool, consistent with edit access already being admin/PM-only. |
| `/catalog` → **Vendors** tab | admin/PM | New tab on the existing Catalog page (alongside Cost items/Markup profiles/Branding). Simple list + create/edit form for `Vendor` (name, contact email, contact phone, notes) — no dedicated top-level nav item, since this is infrequent setup data, not a daily-use screen. |

`frontend/middleware.ts`'s matcher extends to cover `/materials/:path*`.

## Decision 6: BFF Route Handlers

Thin Next.js Route Handlers, one per backend interaction, following the established `bearerToken(request) → apiFetch → errorResponse` pattern:

- `api/materials` (GET, company-wide list with status/project filters, cursor-paginated)
- `api/projects/[id]/materials` (GET project-scoped list, POST manual line create)
- `api/materials/[id]` (PATCH — mark ordered + vendor assignment)
- `api/materials/[id]/receipts` (POST — record a receipt)
- `api/vendors` (GET list, POST create), `api/vendors/[id]` (PATCH edit)

## Decision 7: Access control and tier gating

All BOM/Materials/Vendor routes require the `"estimation"` module (`tier_allows(..., "estimation")`), matching every other route in this domain — no new tier dimension. Read access to `/materials` and the project Materials tab, and all write access (mark ordered, record receipt, add manual line, manage vendors), is admin/PM only. No client or field_crew visibility in this sub-project — if a future need for client-visible order status emerges, that's new scope for a later spec, not assumed here.

## Testing

Backend: tenant-isolation regression tests for `Vendor`/`BomLine`/`BomLineReceipt` (matching the pattern every prior sub-project's Decision-N table gets), a dedicated test for the auto-generation/merge handler (single estimate creates lines; a second approved estimate on the same project merges quantities for a shared catalog item; bare-lead estimate approval no-ops), and route tests for the new endpoints under both allowed and denied roles/tiers.

Frontend: extend the Playwright E2E suite with a Materials arc — approve an estimate (reusing the existing estimation flow), assert BOM lines appear on the project's Materials tab, mark a line ordered with a vendor, record a partial receipt and assert the status reads "Partially received," record the remainder and assert "Received," and confirm the same line surfaces correctly filtered on the global `/materials` page.

## Out of scope (deliberate)

- **AP/Invoicing integration** — marking a line ordered/received never creates a Bill or Expense; that stays a manual, separate action.
- **Client or field_crew visibility** — Materials is admin/PM only in this sub-project.
- **Vendor as a first-class CRM/AP entity** — no linkage to future Bill records, no purchase-order documents, no vendor-side portal. It's a lightweight directory for BOM-line attribution only.
- **Retroactive BOM generation** — if an estimate was approved before this feature existed, its BOM lines are not backfilled; only future approvals generate lines. (No backend event exists to trigger backfill without a one-off migration script, which is out of scope here.)
- **Editing/deleting individual receipts** — a receipt, once recorded, is permanent ledger history; correcting a mis-entered quantity is a new receipt (positive or negative), not an edit — consistent with how `InvoicePayment`/`BillPayment` are treated elsewhere in this codebase. If a negative-quantity correction path turns out to be needed, that's a small follow-up, not blocking this spec.
