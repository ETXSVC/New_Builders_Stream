# Compliance Tracking — Design Spec

**Date:** 2026-07-13
**Depends on:** Phase 0 (foundation), Phase 1 (CRM & Project Management), Phase 2 (Estimation Engine + E-Signature) — merged to `main`.
**Scope source:** [`docs/02-functional-requirements.md`](../../02-functional-requirements.md) Section 7 (US-7.1–US-7.3), [`docs/04-database-schema.md`](../../04-database-schema.md) Section 6, [`docs/05-api-specification.md`](../../05-api-specification.md) Section 8, [`docs/07-security-compliance.md`](../../07-security-compliance.md) Section 8, [`docs/09-roadmap-implementation-plan.md`](../../09-roadmap-implementation-plan.md) Phase 3.

## Decomposition note

`docs/09-roadmap-implementation-plan.md`'s "Phase 3" bundles three areas for a *business* reason (all needed before Enterprise-tier onboarding): Compliance Tracking, Builders Stream's own Stripe subscription billing, and client-facing invoicing/expenses/profitability reporting. `docs/02-functional-requirements.md`'s own business rules explicitly say the billing and invoicing flows "must not be conflated in the data model" — these three areas are far more loosely coupled to each other than Phase 1's CRM+Project-Management pairing or Phase 2's Estimation+E-Signature pairing were. Per an explicit user decision, these are being planned and built as **three separate specs/plans, one at a time**, not one combined "Phase 3" plan. This spec covers **Compliance Tracking only** — the smallest and most self-contained of the three. Billing and Invoicing/Expenses/Profitability are each their own future spec.

## Scope

**Backend-only**, matching the established precedent from every prior phase (the frontend remains a minimal health-check scaffold; frontend UI work is a separate, later, cross-phase effort). This spec covers the API, data model, background job, and test coverage — no frontend screens.

## 1. Data Model

Three tables already pre-sketched in `docs/04-database-schema.md` Section 6, built as-is:

```sql
CREATE TABLE subcontractors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id),
    name VARCHAR(255) NOT NULL,
    trade VARCHAR(100),
    contact_email VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE compliance_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subcontractor_id UUID NOT NULL REFERENCES subcontractors(id) ON DELETE CASCADE,
    company_id UUID NOT NULL REFERENCES companies(id),
    doc_type VARCHAR(30) NOT NULL CHECK (doc_type IN ('insurance_certificate','license')),
    storage_path TEXT NOT NULL,
    expires_on DATE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_compliance_expiry ON compliance_documents(company_id, expires_on);

CREATE TABLE subcontractor_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    subcontractor_id UUID NOT NULL REFERENCES subcontractors(id),
    company_id UUID NOT NULL REFERENCES companies(id),
    assigned_by UUID NOT NULL REFERENCES users(id),
    override_reason TEXT, -- populated only when assigned despite an expired compliance doc
    created_at TIMESTAMPTZ DEFAULT now()
);
```

One new table, not in the existing schema doc, required by the "persisted + dismissible" notification decision below:

```sql
CREATE TABLE compliance_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id),
    compliance_document_id UUID NOT NULL REFERENCES compliance_documents(id) ON DELETE CASCADE,
    threshold VARCHAR(10) NOT NULL CHECK (threshold IN ('30_day','14_day','7_day')),
    fired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ,
    UNIQUE (compliance_document_id, threshold)
);
```

- The `UNIQUE (compliance_document_id, threshold)` constraint is the actual correctness guarantee against duplicate notifications — a DB-level guarantee, not application discipline alone.
- "Expired" is **not** a fourth notification threshold. The compliance dashboard (`GET /compliance/dashboard`) computes "expiring soon" and "already expired" live from `compliance_documents.expires_on` on every request — only the 30/14/7-day *alerts* need to persist, so they're dismissible and don't re-fire daily once acknowledged.
- No update/delete route exists for `compliance_documents` (immutability by omission, matching `esignatures`/`daily_logs`'s own established pattern) — a wrong expiry date is corrected by uploading a new document, not editing the old one, consistent with the 7-year retention rule (`docs/07-security-compliance.md` Section 7).
- All four tables get the ordinary, non-inherited `get_all_descendant_ids()`-only `tenant_isolation` RLS policy shape already used by `markup_profiles`/`estimates`/`change_orders` — no new isolation pattern needed.

## 2. API Surface

| Route | Method | Role | Notes |
|---|---|---|---|
| `/subcontractors` | POST | admin | Create |
| `/subcontractors` | GET | admin, project_manager, accountant | Paginated list |
| `/subcontractors/{id}` | GET | admin, project_manager, accountant | Detail |
| `/subcontractors/{id}/compliance-documents` | POST | admin | Attach insurance cert or license |
| `/subcontractors/{id}/compliance-documents` | GET | admin, project_manager, accountant | List docs for one subcontractor |
| `/compliance/dashboard` | GET | admin, project_manager, accountant | Company-wide expiring/expired list, computed live |
| `/compliance/notifications` | GET | admin only | Persisted 30/14/7-day alerts — US-7.1 specifies these go "to the Admin" |
| `/compliance/notifications/{id}/dismiss` | POST | admin only | Sets `read_at` |
| `/projects/{id}/subcontractor-assignments` | POST | admin, project_manager | See override rule, Section 4 |
| `/projects/{id}/subcontractor-assignments` | GET | admin, project_manager, accountant | List assignments for a project |

RBAC matches `docs/07-security-compliance.md`'s existing "Compliance" row exactly: Admin = Full CRUD, Project Manager = Read + assign (with override logging), Field Crew = none, Accountant = Read, Client = none.

## 3. Scheduling Architecture

Dramatiq has no built-in cron/periodic scheduling — "when to run" and "how to run" are kept as separate concerns, the same way the existing async-job infra (Task 2.14/2.15) already separates the HTTP request that enqueues a PDF export from the worker that generates it.

**Chosen approach:** a new, small `scheduler` Docker Compose service running `APScheduler` in a single Python process, whose only job is to call `.send()` once daily on a new `check_compliance_expiry` Dramatiq actor. The actual scan-and-notify work runs in the *existing* `worker` service, inheriting Dramatiq's retry/backoff/isolation-from-request-timing for free.

Two alternatives considered and rejected:
- **`dramatiq-crontab`** (a third-party addon bundling cron scheduling into a Dramatiq actor decorator, running inside the worker process) — less-adopted than a plain APScheduler-enqueue split, and mixing a scheduler thread into Dramatiq's own multi-process worker model is more fragile than keeping the two concerns in separate processes.
- **OS/Docker-level cron hitting an internal HTTP endpoint** — couples the trigger to HTTP reachability, needs its own internal-only auth scheme distinct from user JWTs, and discards the retry/backoff every other async job in this codebase already gets from Dramatiq, for no real benefit.

**`check_compliance_expiry` actor behavior:** for every company, for every `compliance_documents` row whose `expires_on` crosses a 30/14/7-day threshold as of today, insert a `compliance_notifications` row if one doesn't already exist for that `(document, threshold)` pair. Runs once daily. A missed run is not a lost notification — the next run's query is `expires_on - today <= threshold`, not `== threshold` exactly, so a late run still catches anything it should have caught the day before.

Following this codebase's established "test calls the undecorated function, the route/schedule calls the Dramatiq-wrapped actor" split (Task 2.15's own precedent for `estimate_pdf`), the actor is implemented as a plain, undecorated `async def _check_compliance_expiry(...)` that tests call directly, with a thin `check_compliance_expiry = dramatiq.actor(...)` wrapper that the scheduler's `.send()` call uses.

## 4. Business Rules & Error Handling

- **Expired-vs-absent documents**: only a `compliance_documents` row that literally exists and has `expires_on < today` blocks assignment. A subcontractor with **zero** documents uploaded is **not** blocked — this is the literal reading of US-7.3's "expired-insurance Subcontractor" wording, resolved explicitly as a deliberate interpretation (not an oversight) after considering and rejecting the stricter "treat absent as blocking too" alternative.
- **Assignment override flow** (`POST /projects/{id}/subcontractor-assignments`): one route, reachable by both `admin` and `project_manager`, with a conditional check inside the handler — the same "business-rule check lives in the router, not a blanket role gate" pattern already established for the Change-Order-blocks-Project-completion check (Task 2.23). If the target subcontractor has any expired required document: a `project_manager` caller gets 409; an `admin` caller must supply a non-empty `override_reason` (422 otherwise), and the resulting assignment writes an audit log entry (`subcontractor.assigned_with_expired_docs`, the exact action name already anticipated in `docs/04-database-schema.md`'s own audit_log comment). A compliant subcontractor requires neither admin role nor a reason from either caller.
- **File upload**: `compliance-documents` reuses the exact `validate_file_name`/exclusive-create pattern from `app/services/document_storage.py`, via a new sibling `write_compliance_document_file` function following that file's own documented "each `write_*_file` function differs in small, explained ways" convention.
- **Notification delivery**: in-app only for this spec (a `compliance_notifications` row + the two GET/POST routes above). No email-sending integration exists in this codebase yet; real email delivery is an explicitly deferred future enhancement, not a silent scope cut — same category of deferral as the WeasyPrint→xhtml2pdf substitution in Task 2.13.

## 5. Testing

Same discipline as every prior phase:
- Full CRUD/RBAC test coverage per route.
- A tenant-isolation regression extension (cross-tenant 404, header-spoofing rejection, one RLS-disable/re-enable proof — `compliance_documents` or `subcontractors` is representative of the shared plain policy shape, matching the "prove the mechanism once per distinct shape" precedent from Tasks 2.6/2.16/2.24).
- The scheduler actor gets its own direct test (calling `_check_compliance_expiry` as a plain async function, not through the full broker/worker round-trip — same split Task 2.15 established), covering: multiple companies with no cross-tenant leakage, all three thresholds firing correctly, no duplicate notification on a second run against the same data, and the "missed a day, still catches it" late-run behavior.
- The override flow gets explicit coverage for all four combinations (PM/expired, PM/compliant, Admin/expired-with-reason, Admin/expired-without-reason).
