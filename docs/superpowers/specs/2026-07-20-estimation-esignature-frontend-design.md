# Estimation + E-Signature Frontend — Design Spec

**Date:** 2026-07-20
**Depends on:** CRM+PM frontend (PR #18, merged to `main` at 28f318d). Builds on Foundation's BFF session architecture and the CRM+PM screen conventions (AppShell nav, tab shells, state-machine mirrors, fetch-with-bearer download pattern).
**Sub-project:** 3 of 6 in the frontend build-out (Foundation → CRM+PM → **Estimation+E-Signature** → Compliance+Billing → Invoicing+Reporting → Integrations+Admin).

## Goal

Product screens for the estimation domain: cost catalog + markup profile management (with CSV import/export and parent-company override support), an estimate builder with live calculation, PDF export with per-company branding, an in-app typed e-signature flow for clients (estimates and change orders), and change-order management on projects. The estimation module is tier-gated at `pro`; fresh trials are created at `pro`, so registration-driven flows (including E2E) can use the whole domain.

## Decision 1: Backend additions — close every gap

The user chose to close all API gaps, not just blockers. Every addition follows the domain's existing conventions: same role dependencies, `require_module("estimation")` + `block_if_read_only` on writes, tenant scoping, cursor pagination on lists, audit-log entries on mutations.

1. **`GET /estimates/{estimate_id}/pdf`** — streams the exported PDF from `pdf_storage_path` with `Content-Disposition` attachment. Roles mirror estimate detail (admin/PM/accountant/client). 409 unless `pdf_status == "ready"`. Read route: no tier gate, matching every existing GET.
2. **`PATCH /catalogs/items/{id}`** — edit `category`/`name`/`unit`/`unit_rate` (all optional fields). Admin/PM. Does not touch existing estimates: line items keep their `unit_rate_snapshot`.
3. **`DELETE /catalogs/items/{id}`** — admin/PM. 409 if the item is referenced by any estimate line item **or** has child-company overrides (`parent_catalog_item_id` pointing at it); otherwise hard delete.
4. **`PATCH /markup-profiles/{id}`** and **`DELETE /markup-profiles/{id}`** — admin/PM. Delete 409s if any estimate references the profile.
5. **`PATCH /estimates/{estimate_id}`** — admin/PM, **draft-only** (409 otherwise). Only `markup_profile_id` is editable; the project/lead binding is immutable after creation.
6. **`DELETE /estimates/{estimate_id}`** — admin/PM, **draft-only** (409 otherwise). Deletes the estimate and its line items.
7. **`GET /change-orders/{change_order_id}`** — single change order. Roles mirror the per-project list (admin/PM/accountant/client).
8. **`GET /change-orders`** — company-wide list, cursor-paginated, `?status=` filter. Admin/PM/accountant see all; `client` is scoped to `status="pending"` (mirroring the per-project list). This is what feeds the client's "awaiting your signature" card without N per-project calls. Rows are enriched with `project_name`.
9. **`parent_name` on estimate list rows** — `EstimateListResponse` rows gain a `parent_name` string (the linked project's name or lead's project name) via join, the same enrichment pattern `GET /tasks?assignee=me` already uses. Without it the global estimates list can only show UUIDs.

After all backend work (including Decisions 8–9 below), `frontend/lib/api/types.ts` is regenerated from the live `/openapi.json`.

## Decision 2: Routes and information architecture

Estimates are reachable both globally and contextually (user-selected option C), sharing one detail page. All new pages live in the `(app)` route group; `frontend/middleware.ts`'s matcher adds `/estimates/:path*` and `/catalog/:path*`.

| Route | Roles | Content |
|---|---|---|
| `/estimates` | admin/PM/accountant | Cross-company list: status filter, rows show status badge · parent (project/lead) name · total · created date. "New estimate" opens creation (pick a project or lead). |
| `/estimates/[id]` | admin/PM/accountant (full), client (sent+approved views) | The state-driven detail page (Decision 3). |
| `/catalog` | admin/PM (edit), accountant (read) | New nav item, three tabs: **Cost items**, **Markup profiles**, **PDF template** (Decision 8; PDF template tab is admin-only). |
| Project detail → **Estimates** tab | staff | This project's estimates (filtered rows linking to `/estimates/[id]`), "New estimate" pre-bound to the project. |
| Project detail → **Change orders** tab | staff | Decision 6. |
| Lead detail → **Estimates** section | admin/PM | Same filtered rows pre-bound to the lead, below the communication log. |
| Client project view | client | `ClientProjectDashboard` gains an **"Awaiting your signature"** card: sent estimates (`GET /estimates?status=sent` — the backend already scopes client lists to sent) and pending change orders (`GET /change-orders`, client-scoped to pending), estimates link to `/estimates/[id]`; change orders expand inline with the signing panel (Decision 6). |

Nav (`components/app-shell/Nav.tsx`): **Estimates** and **Catalog** links for admin/PM/accountant. Client and field_crew nav is unchanged.

## Decision 3: Estimate detail — one page, state-driven

`/estimates/[id]` renders by `status`:

- **`draft`** — the **two-panel builder** (user-selected option B): left panel browses the resolved catalog grouped by category with search (`GET /catalogs/items` already supports `?search=`/`?category=`); clicking **+** adds a line. Right panel lists lines with editable quantity, per-line remove, and a running subtotal/total. **Save chains `PUT /lines` → `POST /calculate`** so persisted totals are always fresh; the category breakdown from the calculate response renders under the lines. Header actions: change markup profile (PATCH), delete (confirm dialog → back to list), PDF export (Decision 5), and **Send for signature** — disabled with an explanatory hint until `total` is non-null (the backend 409s otherwise). Panels stack vertically on mobile.
- **`sent`** — read-only lines + breakdown + PDF panel. Staff see a "waiting for client signature" banner. **Client** sees the PDF and the signing panel (Decision 4): typed-signature approve, or reject with a required reason.
- **`approved`** — locked. Shows the signature record fetched from `GET /esignatures/{esignature_id}` (signer name, email, signed-at, IP) and the PDF panel. A note points to the auto-drafted deposit invoice (Invoicing sub-project will link it properly).
- **`rejected`** — a "Rejected" banner. The rejection reason lives only in the backend's audit log and is not exposed on the estimate response, so the banner carries no reason text (exposing it is deliberately out of scope — no new backend surface for it). Terminal for sending: the backend has no re-send transition, so the offered action is **Duplicate as new draft** (Decision 7).

Estimate creation: from `/estimates` (choose project **or** lead from pickers fed by the existing list endpoints + markup profile select), or pre-bound from the project tab / lead section. Creating navigates straight into the draft builder.

## Decision 4: Typed e-signature (shared component)

User-selected option B (DocuSign-style "adopt"): the signer types their full name, sees it rendered live in a script font, enters their email, and confirms. On submit the preview is drawn to a hidden `<canvas>`, exported via `toBlob()` as PNG, and posted as `multipart/form-data` (`signer_name`, `signer_email`, `signature_artifact`) to the approve route — exactly the shape the backend expects. IP and timestamp are captured server-side. One shared component (`components/esign/TypedSignature.tsx`) serves both estimates and change orders; a wrapping `SigningPanel` adds the approve/reject pair (reject = JSON `{reason}` with a required textarea). No drawn-signature canvas in this sub-project.

## Decision 5: PDF export, polling, inline viewing

The PDF panel on the estimate detail page drives the whole lifecycle from `pdf_status`:

- `not_requested` → "Generate PDF" button → `POST /export` (202).
- `pending` → the page polls `GET /estimates/{id}` every 3 seconds (stops on unmount); after 2 minutes of pending it keeps the spinner but surfaces "still generating — check back shortly".
- `ready` → inline viewer: fetch `/api/estimates/{id}/pdf` with the bearer header → blob → object URL in an `<iframe>`, plus a Download button (same fetch-blob-anchor pattern as document download). Regenerate stays available (re-export after line changes on a draft).
- `failed` → error notice + "Retry export".

## Decision 6: Change orders

Project detail gains a **Change orders** tab: list (status badges, cost delta with sign, schedule impact), and for admin/PM a create form (description, cost delta — positive or negative, schedule impact days). Creation is only legal on `active` projects; the backend 409 is surfaced verbatim. Pending rows offer **Send for signature** (the backend validates but doesn't change state — the UI treats it as "notify/ready" confirmation) and staff see status progress. The **client** signs or rejects a change order directly inside the awaiting-signature card: each pending CO row expands inline to show its details and the shared `SigningPanel` — no separate change-order page exists or is needed (estimates, by contrast, link out to `/estimates/[id]`). The existing `ProjectStatusActions` already surfaces the "Cannot complete project: N Change Order(s) pending approval" 409 — no new work there beyond an E2E assertion.

## Decision 7: Estimate duplication (revisions)

A **"Duplicate as new draft"** action on any estimate (primarily used from `rejected` and `approved`). Pure frontend orchestration: `POST /estimates` with the same parent and markup profile, then `PUT /lines` with the source's `(cost_catalog_item_id, quantity)` pairs, then navigate to the new draft. Rates deliberately re-resolve from the current catalog at calculate time — a revision prices at today's rates while the original keeps its snapshot. No `revision_of` linkage in the model (YAGNI): the lists already show both estimates with statuses.

## Decision 8: PDF template customization (company branding)

Per-company branding applied by the PDF worker — branding, not layout editing (no template designer):

- **Backend:** `company_branding` table (one row per company: `logo_storage_path: str|None`, `accent_color: str` hex with default, `footer_text: str` default empty) + migration; `GET /companies/branding` (admin/PM), `PUT /companies/branding` (admin-only, JSON accent/footer), `POST /companies/branding/logo` (admin-only, multipart image — PNG or JPEG, ≤2 MB, 413/415-style 4xx otherwise — stored under the existing storage root per company); the estimate PDF template renders logo, accent color, and footer when present. Missing row = defaults (current appearance).
- **Frontend:** the **PDF template** tab on `/catalog` (admin-only): logo upload with preview, accent color input, footer/terms textarea, and a note that changes affect future exports only (generated PDFs are immutable artifacts).

## Decision 9: Catalog CSV import/export

- **Backend:** `POST /catalogs/items/bulk` — JSON `{items: [CostCatalogItemCreateRequest, …]}`, max 500 rows, admin/PM, estimation tier. Validates per row and returns `{results: [{index, status: "created"|"error", detail?}]}` so one bad row doesn't fail the file.
- **Frontend:** on the Cost items tab, **Import CSV** parses a documented four-column format (`category,name,unit,unit_rate`, header row required) with a small hand-rolled parser in `lib/csv.ts` (handles quoted fields and CRLF; no new dependency), shows a preview table with per-row client-side validation (files over 500 rows are refused with a "split the file" message rather than silently truncated), submits the batch, and renders the per-row result report. **Export CSV** serializes the already-loaded resolved item list client-side to the same format (for offline editing, re-import, or seeding a child company).

## Decision 10: BFF Route Handlers

One thin handler per backend interaction, following the established `bearerToken → apiFetch → errorResponse` pattern (multipart and PDF-stream handlers use raw fetch + `BACKEND_API_URL`, like document upload/download):

- `api/estimates` (GET, POST), `api/estimates/[id]` (GET, PATCH, DELETE), `api/estimates/[id]/lines` (PUT), `.../calculate` (POST), `.../export` (POST), `.../pdf` (GET stream), `.../send-for-signature` (POST), `.../approve` (POST multipart pass-through), `.../reject` (POST)
- `api/esignatures/[id]` (GET)
- `api/catalog/items` (GET, POST), `api/catalog/items/bulk` (POST), `api/catalog/items/[id]` (PATCH, DELETE), `api/catalog/items/[id]/override` (POST)
- `api/markup-profiles` (GET, POST), `api/markup-profiles/[id]` (PATCH, DELETE)
- `api/projects/[id]/change-orders` (GET, POST), `api/change-orders` (GET), `api/change-orders/[id]` (GET), `.../send-for-signature` (POST), `.../approve` (POST multipart), `.../reject` (POST)
- `api/companies/branding` (GET, PUT), `api/companies/branding/logo` (POST multipart)

## Decision 11: Components

- `components/estimates/` — `EstimateBuilder` (two-panel draft editor), `CatalogPanel` (category accordion + search + add), `LineRows` (qty edit, remove, subtotal), `CategoryBreakdown`, `PdfPanel` (Decision 5), `EstimateRows` (shared list rows for global list / project tab / lead section), `NewEstimateForm`, `DuplicateButton`
- `components/esign/` — `TypedSignature`, `SigningPanel` (approve + reject-with-reason)
- `components/change-orders/` — `ChangeOrdersTab`, `ChangeOrderForm`, `ChangeOrderRows`
- `components/catalog/` — `CatalogItemsTab`, `CatalogItemForm` (create/edit/override), `MarkupProfilesTab`, `MarkupProfileForm`, `CsvImport` (file input → preview → report), `BrandingTab`
- `lib/csv.ts` — parse/serialize for the four-column format

Existing primitives (Button, Input, Label, Select, Textarea, StatusBadge, Card) are reused; no new primitives anticipated and **zero new dependencies**. All screens follow the established hardening conventions: `submitting` guards, try/catch network messages, `aria-live` errors, `role`-conditional actions with the backend as the sole authz boundary, and cursor pagination followed to exhaustion where lists back a "must see the new row" flow.

## Decision 12: Errors, read-only mode, empty states

Same conventions as CRM+PM: 403/409 details render verbatim inline (notably: send-before-calculate, edit-after-snapshot, delete-with-references, CO-on-inactive-project, completion-block); read-only-mode write blocks surface as returned; every list has an empty state with a create prompt; catalog delete asks for confirmation and explains the 409 when references exist.

## Decision 13: Testing — Playwright E2E as proof-of-done

New spec `e2e/estimation.spec.ts` against the live Compose stack (registrations get `pro` trials, so estimation is available):

1. **Admin arc:** register → Catalog: create items by hand, CSV-import a small batch and assert the per-row report, edit a rate, create a markup profile → create a project → New estimate from the project's Estimates tab → builder: add lines, save (auto-calculate), assert totals + category breakdown → export PDF, poll to `ready`, download and assert it's a non-empty `application/pdf` → send for signature.
2. **Client arc:** seed a client user through the backend invitation API using Playwright's request context (invitation-acceptance UI stays out of scope) → log in as the client → "Awaiting your signature" card shows the estimate → typed-signature approve → assert `approved`, signature record visible, and further edits impossible.
3. **Revision + change order:** admin duplicates the approved estimate and asserts a fresh draft with copied lines → creates a change order on the active project → attempts project completion and asserts the 409 banner → client approves the CO via their card → completion now succeeds.
4. Branding: admin saves footer text + accent color on the PDF template tab and re-exports (assert export completes; pixel-level PDF inspection stays in backend tests).

Backend: new tests for every route in Decisions 1, 8, 9 (roles, tier, tenant isolation, 409 rules), plus full regression. Frontend: lint, `tsc`, production build, both existing E2E specs still green.

## Out of scope (deliberate)

- **Invitation/user-management UI** — the E2E seeds the client via API; screens arrive with the Admin sub-project.
- **Drawn signatures** — typed-only; the capture component's interface (produce a PNG blob) leaves room to add a draw tab later.
- **Revision-chain modeling** (`revision_of` field, history views) — duplication covers the workflow.
- **PDF layout editing** — branding fields only.
- **Estimates on the client beyond signing** — clients see sent/approved estimates for their project, not a browsing UI.
- **Catalog item images/attachments, price history** — YAGNI.
