# Builders Stream — API Specification

**Version:** 1.0
**Date:** 2026-07-07
**Related:** [Technical Architecture](03-technical-architecture.md) · [Database Schema](04-database-schema.md)

This document describes API contracts conceptually. The authoritative, machine-readable contract is FastAPI's auto-generated `openapi.json`, from which frontend TypeScript types are generated (see [Technical Architecture](03-technical-architecture.md), Section 2). Nothing here should be hand-transcribed into frontend types — always regenerate from the live schema.

## 1. Conventions

- **Base URL:** `https://api.buildersstream.app/v1` (self-hosted equivalent in non-prod environments).
- **Auth:** `Authorization: Bearer <jwt>` on every request except public auth routes.
- **Tenant context:** `X-Tenant-ID: <company_uuid>` required for any user with access to more than one company; optional (inferred from JWT) for single-company users.
- **Pagination:** `?limit=` (default 25, max 100) and `?cursor=` query params; responses include a `next_cursor`.
- **Errors:** JSON body `{ "error": { "code": "string", "message": "string", "details": {...} } }` with standard HTTP status codes (400 validation, 401 unauthenticated, 403 unauthorized/wrong tenant, 404 not found, 409 illegal state transition, 422 unprocessable entity, 429 rate-limited, 5xx server error).
- **Idempotency:** all `POST` endpoints that create billable or externally-synced records accept an optional `Idempotency-Key` header.

## 2. Users & Company Management

| Route | Method | Purpose | Key Inputs |
|---|---|---|---|
| `/auth/register` | POST | Create a new company + first Admin user | Company name, admin email/password |
| `/auth/login` | POST | Authenticate, issue JWT | Email, password |
| `/companies/{id}` | GET | Retrieve company detail | — |
| `/companies/{id}/children` | POST | Create a child branch company | Name, parent implied by path |
| `/companies/{id}/users` | GET | List users in a company | Pagination |
| `/invitations` | POST | Invite a user to a company | Email, role |
| `/invitations/{token}/accept` | POST | Accept an invitation | New user's password (if not existing) |

## 3. CRM

| Route | Method | Purpose | Key Inputs |
|---|---|---|---|
| `/leads` | POST | Create a Lead | company_id, contact_name, project_name, email, phone, project_type |
| `/leads` | GET | List Leads | Pagination, `?status=` filter |
| `/leads/{id}` | GET | Retrieve a Lead | — |
| `/leads/{id}` | PATCH | Update Lead fields / status | Any updatable field |
| `/leads/{id}/communications` | POST | Log a communication | channel, body |
| `/leads/{id}/communications` | GET | List communication history | Pagination |

## 4. Project Management

| Route | Method | Purpose | Key Inputs |
|---|---|---|---|
| `/projects` | POST | Initialize a Project | Name, client/lead_id, site_address, projected_start_date |
| `/projects` | GET | List Projects | Pagination, `?status=` filter |
| `/projects/{id}` | GET | Full project dashboard data | — |
| `/projects/{id}/status` | PATCH | Advance/revert lifecycle state | New status, optional reason |
| `/projects/{id}/phases` | POST | Add a Phase | Name, sequence |
| `/projects/{id}/tasks` | POST | Add a Task | Name, due_date, assignee_id, phase_id |
| `/tasks/{id}` | PATCH | Update task status/assignment | — |
| `/projects/{id}/documents` | POST | Upload a Document (new version if filename matches) | File, file_name |
| `/projects/{id}/daily-logs` | POST | Submit a Daily Log | log_date, weather, notes, photos |
| `/projects/{id}/change-orders` | POST | Create a Change Order | description, cost_delta, schedule_impact_days |
| `/change-orders/{id}/send-for-signature` | POST | Route to client for e-signature | signer_email |

## 5. Estimation Engine

| Route | Method | Purpose | Key Inputs |
|---|---|---|---|
| `/catalogs/items` | GET | List cost catalog items for active branch | Pagination, category filter, search |
| `/catalogs/items` | POST | Create/override a catalog item | category, name, unit, unit_rate |
| `/markup-profiles` | POST | Create a Markup Profile | name, overhead_pct, profit_pct |
| `/estimates` | POST | Initialize an Estimate | project_id or lead_id, markup_profile_id |
| `/estimates/{id}/lines` | PUT | Batch replace line items | Array of `{cost_catalog_item_id, quantity}` |
| `/estimates/{id}/calculate` | POST | Server-side recalculation | — (uses saved state) |
| `/estimates/{id}/export` | POST | Queue branded PDF generation | Target email, template_id → returns `202 Accepted` |
| `/estimates/{id}/send-for-signature` | POST | Route to client for e-signature | signer_email |
| `/esignatures/{id}` | GET | Retrieve signature record (audit) | — |

## 6. Accounting & Billing — AR/AP (Post-MVP)

| Route | Method | Purpose | Key Inputs |
|---|---|---|---|
| `/subscriptions/me` | GET | Current company's Builders Stream plan | — |
| `/subscriptions/portal-session` | POST | Create a Stripe Customer Portal session | — |
| `/projects/{id}/invoices` | POST | Create a client-facing invoice (AR) | amount, due_date (optional) |
| `/projects/{id}/invoices` | GET | List a project's invoices (Client: non-draft only) | — |
| `/invoices/{id}` | GET | Invoice detail, payments, outstanding balance | — |
| `/invoices/{id}/send` | POST | draft → sent, assigns due_date | due_date (if not already set) |
| `/invoices/{id}/payments` | POST | Record a payment received; auto-marks paid when fully covered | amount, paid_date |
| `/invoices/{id}/void` | POST | Void a non-paid invoice | — |
| `/bills` | POST | Record a vendor Bill (AP); project/subcontractor optional | project_id?, subcontractor_id?, vendor_name?, amount, due_date?, bill_number? |
| `/bills` | GET | List Bills, optionally filtered by project | project_id? |
| `/bills/{id}` | GET | Bill detail, payments, outstanding balance | — |
| `/bills/{id}/payments` | POST | Record a payment made; auto-marks paid when fully covered | amount, paid_date |
| `/bills/{id}/void` | POST | Void a non-paid bill | — |
| `/projects/{id}/expenses` | POST | Record a non-vendor expense | description, amount, incurred_on |
| `/projects/{id}/expenses` | GET | List a project's expenses | — |
| `/reports/profitability` | GET | Per-project billed revenue/cost/profitability (date range) plus company-wide AR aging, AP aging, and estimated tax liability (point-in-time) | Date range |

## 7. External Integrations (Post-MVP)

| Route | Method | Purpose | Key Inputs |
|---|---|---|---|
| `/integrations/{provider}/connect` | GET | Begin OAuth flow — returns `{"authorization_url": "..."}` as JSON (not a raw redirect; the caller is responsible for navigating there) | — |
| `/integrations/{provider}/callback` | GET | OAuth callback, exchanges `code` for tokens and stores the connection. No `CurrentUser` — the signed `state` parameter carries the authenticated `company_id` instead. | code, state |
| `/integrations/{provider}/sync-status` | GET | Connection summary plus a paginated, per-record sync status list (`?status=` filter supported) | cursor, limit, status |

`provider` is `quickbooks` or `freshbooks`, per `integration_connections.provider`'s own CHECK constraint (Section 7 of the [Database Schema](04-database-schema.md)). See [`docs/superpowers/specs/2026-07-15-integrations-quickbooks-freshbooks-design.md`](superpowers/specs/2026-07-15-integrations-quickbooks-freshbooks-design.md) for the full design — this phase builds the provider-agnostic core behind a fake client, not real QuickBooks/FreshBooks wiring.

## 8. Compliance

| Route | Method | Purpose | Key Inputs |
|---|---|---|---|
| `/subcontractors` | POST | Add a Subcontractor/Vendor | name, trade, contact_email |
| `/subcontractors/{id}/compliance-documents` | POST | Attach an insurance cert or license | doc_type, file, expires_on |
| `/compliance/dashboard` | GET | Company-wide expiring/expired doc list | — |
| `/projects/{id}/subcontractor-assignments` | POST | Assign a subcontractor to a project | subcontractor_id, override_reason (if compliance expired) |

## 9. Webhooks (Inbound)

| Route | Source | Purpose |
|---|---|---|
| `/webhooks/stripe` | Stripe | Subscription lifecycle events (payment succeeded/failed, plan changed) |
| `/webhooks/esignature-provider` | E-signature provider (if a third-party service is used instead of a built-in flow) | Signature completed/declined |

## 10. Security Requirements on Every Endpoint

- Every route (except `/auth/*` and public webhook receivers) requires a valid JWT resolving to an active `company_users` membership with sufficient role, enforced by a FastAPI dependency, in addition to the database-level RLS policy (defense in depth — see [Security & Compliance](07-security-compliance.md)).
- Webhook receivers verify provider signatures (e.g., Stripe's `Stripe-Signature` header) before processing.
