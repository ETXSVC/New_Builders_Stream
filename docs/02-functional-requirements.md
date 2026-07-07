# Builders Stream — Functional Requirements Specification

**Version:** 1.0
**Date:** 2026-07-07
**Related:** [PRD](01-prd.md) · [Technical Architecture](03-technical-architecture.md) · [Database Schema](04-database-schema.md)

Each module lists its core entities, user stories with acceptance criteria, and business rules. Modules are tagged with their implementation priority from the [Roadmap](09-roadmap-implementation-plan.md): **P0** (foundation), **P1–P2** (MVP), **P3–P4** (post-MVP).

---

## 1. Users & Company Management (P0)

### Core Entities
Company (with optional `parent_id` for nested branches), User, CompanyUser (role assignment), Invitation.

### User Stories

**US-1.1** — As a company owner, I can create a company account and become its first Admin.
- Acceptance: A new Company row is created; the creating user is assigned the `admin` role via CompanyUser.

**US-1.2** — As an Admin, I can invite additional users to my company with a specific role.
- Acceptance: An invitation email is sent; accepting it creates a CompanyUser row with the assigned role; invitations expire after 7 days.

**US-1.3** — As an Admin of a parent company, I can create child branch companies and view aggregated data across all of them.
- Acceptance: Child companies inherit no data by default but are visible to parent-company admins per the recursive RLS policy (see [Technical Architecture](03-technical-architecture.md), Section 5).

**US-1.4** — As any authenticated user, I can only see and act on data belonging to my company (and its children, if I have parent-level access).
- Acceptance: Verified by automated RLS isolation tests (see [Test Strategy](10-test-strategy.md), Section 2).

### Roles (RBAC)

| Role | Scope |
|---|---|
| Admin | Full CRUD across all modules within their company (and children, if parent). |
| Project Manager | Full CRUD on CRM, Projects, Tasks, Estimates within their company. No billing/subscription access. |
| Field Crew | Read assigned tasks; create Daily Logs and upload photos. No access to budgets or client contact financial data. |
| Accountant | Full access to Billing/Accounting module; read-only elsewhere. |
| Client (external) | Read-only sanitized project dashboard; can approve/reject Estimates and Change Orders via e-signature. |

Full permission matrix is defined in [Security & Compliance](07-security-compliance.md), Section 2.

### Business Rules

- A user may belong to more than one company (e.g., a subcontractor working with multiple clients), but each membership has its own role.
- Deactivating a company (`is_active = false`) cascades to block login for all its users but does not delete data.

---

## 2. CRM (P1)

### Core Entities
Lead, Communication Log (notes/emails/calls linked to a Lead).

### User Stories

**US-2.1** — As a Project Manager, I can create a new Lead with contact info, project type, and an estimated value.
- Acceptance: Lead is created with status `new`; validation matches the schema in [API Specification](05-api-specification.md), Section 2.

**US-2.2** — As a Project Manager, I can move a Lead through the pipeline: New → Contacted → Estimating → Qualified → Won/Lost.
- Acceptance: Status transitions are logged with a timestamp and the acting user.

**US-2.3** — As a Project Manager, when I mark a Lead "Won," a Project is automatically drafted with the client details carried over.
- Acceptance: A `PROJECT_DRAFTED` event fires; a new Project row is created in `draft` status referencing the originating Lead.

**US-2.4** — As any CRM user, I can log a communication (call, email, note) against a Lead and see a chronological history.
- Acceptance: Communication Log entries are immutable once saved (edits create a new entry, not an overwrite).

### Business Rules

- A Lead cannot be deleted once it has an associated Estimate or Project; it can only be marked "Lost."
- Lead status "Estimating" is the trigger point that allows an Estimate to be created against it (see Section 4).

---

## 3. Project Management (P1)

### Core Entities
Project, Phase/Milestone, Task, Document, Daily Log.

### User Stories

**US-3.1** — As a Project Manager, I can define Phases (e.g., "Site Prep," "Foundation," "Framing") and add Tasks within each Phase.
- Acceptance: Tasks require a name, due date, and assignee; they belong to exactly one Phase.

**US-3.2** — As a Project Manager, I can advance a Project through its lifecycle: Draft → Pre-Construction → Active → Suspended → Completed → Archived.
- Acceptance: Illegal transitions (e.g., Draft → Completed) are rejected by the backend state machine.

**US-3.3** — As Field Crew, I can view my assigned Tasks and submit a Daily Log (weather, progress notes, photos) for a Project.
- Acceptance: Daily Logs are timestamped, author-attributed, and immutable once submitted.

**US-3.4** — As a Project Manager, I can upload and organize Documents (blueprints, permits, inspection reports) against a Project.
- Acceptance: Documents support versioning; the most recent version is shown by default with prior versions accessible.

**US-3.5** — As a Client, I can view a read-only dashboard showing my project's progress, milestones, and approved photos, without seeing budget or internal task detail.
- Acceptance: Client-facing view excludes financial fields (verified by RBAC scoping — see [Security & Compliance](07-security-compliance.md)).

**US-3.6** — As a Project Manager, I can create a Change Order against an active Project and route it to the Client for e-signature approval.
- Acceptance: Change Order requires a description, cost delta, and schedule impact; it is not binding until the client's e-signature is captured (see Section 4 and [Security & Compliance](07-security-compliance.md), Section 6).

### Business Rules

- Hierarchical visibility: a user with parent-branch access sees Projects across all child branches; a child-branch user sees only their own branch's Projects.
- A Project cannot move to "Completed" while it has open (non-approved) Change Orders.

---

## 4. Estimation Engine (P1/P2 — MVP)

### Core Entities
Estimate (Quote), Cost Catalog Item, Line Item, Markup Profile.

### User Stories

**US-4.1** — As a Project Manager, I can create a blank Estimate against a Lead or Project and select a Markup Profile.
- Acceptance: Estimate starts with zero line items and a status of `draft`.

**US-4.2** — As a Project Manager, I can add Line Items to an Estimate by selecting Cost Catalog items and specifying quantities.
- Acceptance: Each Line Item's total is computed server-side as `quantity × unit_rate`; client-submitted totals are never trusted.

**US-4.3** — As a Project Manager, I can trigger a recalculation that applies category subtotals, overhead markup, profit margin, and tax in a fixed order (see [Technical Architecture](03-technical-architecture.md), Section 6) to produce a final total.
- Acceptance: All monetary math uses fixed-point decimal arithmetic; no floating-point rounding errors.

**US-4.4** — As a Project Manager, I can export an Estimate as a branded PDF proposal and send it to the Client.
- Acceptance: PDF generation is asynchronous (background job); the requester receives a `202 Accepted` and is notified when the PDF is ready.

**US-4.5** — As a Client, I can review an emailed Estimate and approve it with an e-signature, or reject it with a reason.
- Acceptance: Approval captures a timestamp, IP address, and signature artifact sufficient to satisfy the ESIGN Act's intent-to-sign requirement (see [Security & Compliance](07-security-compliance.md), Section 6).

**US-4.6** — As an Admin, I can define company-wide (or branch-specific) Cost Catalog items and Markup Profiles, with child branches able to override inherited values.
- Acceptance: A child branch's local override takes precedence over the parent's catalog entry for that item; the parent catalog itself is unaffected.

### Business Rules

- **Historical immutability:** once an Estimate is Approved (client-signed) or marked Won, its line items are snapshotted and become immutable, even if the underlying Cost Catalog price later changes.
- An Estimate can only be created against a Lead in "Estimating" status or an existing Project.
- Approving an Estimate emits an `ESTIMATE_APPROVED` event consumed by the Accounting/Billing module (Section 5) to draft the initial invoice.

---

## 5. Accounting & Billing (P3 — Post-MVP)

### Core Entities
Subscription (company's Builders Stream plan), Invoice, Payment, Expense.

### User Stories

**US-5.1** — As an Admin, I can view and manage my company's Builders Stream subscription (plan tier, seats, billing history) via a Stripe-hosted customer portal.

**US-5.2** — As an Accountant, when a Project Estimate is approved, a draft client-facing Invoice is automatically generated for the deposit amount.

**US-5.3** — As an Accountant, I can record Expenses against a Project and see running actual-vs-estimated cost.

**US-5.4** — As an Accountant, I can view a financial report showing project profitability, outstanding invoices, and tax liability across the company.

### Business Rules

- Builders Stream subscription billing (what the contractor pays to use the platform) and client-facing Project invoicing (what the contractor bills their own customer) are separate financial flows and must not be conflated in the data model.
- Financial records are never overwritten; corrections are made via new, linked entries (append-only ledger discipline).

---

## 6. External Integrations — QuickBooks / FreshBooks (P4 — Post-MVP)

### User Stories

**US-6.1** — As an Accountant, I can connect my company's QuickBooks or FreshBooks account via OAuth.

**US-6.2** — As an Accountant, when an Invoice or Expense is created/updated in Builders Stream, it syncs to the connected accounting platform asynchronously, with retry on transient failure and a visible sync-status indicator.

### Business Rules

- Sync failures must not block core Builders Stream workflows — the sync runs as a background task, decoupled from the request/response cycle (see [Technical Architecture](03-technical-architecture.md), Section 4).

---

## 7. Compliance Tracking (Cross-Cutting)

Spans Users & Company (subcontractor/vendor records), Project Management (change order e-signatures), and Estimation (estimate e-signatures).

### User Stories

**US-7.1** — As an Admin, I can add Subcontractors/Vendors to my company with attached insurance certificates and license documents, each with an expiration date.
- Acceptance: The system sends a notification to the Admin 30/14/7 days before an insurance certificate or license expires.

**US-7.2** — As an Admin, I can see a company-wide compliance dashboard listing all subcontractors with expiring or expired documentation.

**US-7.3** — As a Project Manager, I cannot assign an expired-insurance Subcontractor to an active Project without an explicit Admin override (logged).
- Acceptance: Attempting the assignment surfaces a warning; proceeding requires Admin role and is recorded in the audit log.

### Business Rules

- E-signature events (Estimate approval, Change Order approval) are treated as legal compliance records and are retained per the data retention policy in [Security & Compliance](07-security-compliance.md), Section 7 — they are never deleted, only superseded.
