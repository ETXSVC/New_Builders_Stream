# Compliance Tracking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Each task below is dispatched as its own implementer subagent, with genuinely ambiguous judgment calls pre-resolved in the dispatch prompt, followed by an independent spec-compliance review and a code-quality review before the task is considered done, matching the discipline already used for Phases 0–2.

**Goal:** Subcontractor/Vendor compliance tracking — insurance/license document upload with expiry dates, a company-wide compliance dashboard, expiry notifications (30/14/7 days before), and an Admin-override-required assignment rule blocking Project Managers from assigning subcontractors with expired documentation.

**Architecture:** Four new tables (`subcontractors`, `compliance_documents`, `subcontractor_assignments`, `compliance_notifications`) sharing the ordinary `get_all_descendant_ids()`-only RLS policy shape already used by `markup_profiles`/`estimates`/`change_orders`. A new lightweight `scheduler` Docker Compose service (APScheduler) enqueues a daily Dramatiq message onto the existing `worker` service, which scans for newly-crossed 30/14/7-day expiry thresholds and writes idempotent notification rows.

**Tech Stack:** Same as Phases 0–2 (FastAPI, SQLAlchemy async, PostgreSQL + RLS, Alembic, Dramatiq + Redis) plus `apscheduler` (new dependency, scheduler service only — not added to the `backend`/`worker` image's own dependency set).

**Depends on:** Phase 0, Phase 1, Phase 2 — all merged to `main`.

**Scope source:** [`docs/superpowers/specs/2026-07-13-compliance-tracking-design.md`](../specs/2026-07-13-compliance-tracking-design.md) (the approved design spec this plan implements), [`docs/02-functional-requirements.md`](../../02-functional-requirements.md) Section 7, [`docs/04-database-schema.md`](../../04-database-schema.md) Section 6, [`docs/05-api-specification.md`](../../05-api-specification.md) Section 8, [`docs/07-security-compliance.md`](../../07-security-compliance.md) Section 8.

**Decomposition note:** This plan covers Compliance Tracking only — one of the three areas the roadmap's "Phase 3" bundles together. Builders Stream's own Stripe subscription billing, and client-facing invoicing/expenses/profitability reporting, are each their own future spec + plan, per an explicit user decision to plan and build these three areas one at a time rather than as one combined "Phase 3" plan (see the design spec's own Decomposition note for the full rationale).

---

## Task 3.1: Subcontractor, ComplianceDocument, SubcontractorAssignment, ComplianceNotification Models

**Files:** Create `backend/app/models/subcontractor.py`, `backend/app/models/compliance_document.py`, `backend/app/models/subcontractor_assignment.py`, `backend/app/models/compliance_notification.py`. Modify `backend/app/models/__init__.py`.

- `Subcontractor`: `id`, `company_id` (FK, no ondelete, matching every other model's own `company_id` convention), `name` (`String(255)`, not null), `trade` (`String(100)`, nullable), `contact_email` (`String(255)`, nullable), `created_at` (`TimestampMixin` only — no `updated_at` column in the schema doc's own DDL, matching `Phase`'s precedent for models the schema doc gives no `updated_at` column to).
- `ComplianceDocument`: `id`, `subcontractor_id` (FK, `ondelete="CASCADE"`, matching the schema doc's `ON DELETE CASCADE`), `company_id` (FK, no ondelete), `doc_type` (`String(30)`, not null, `CheckConstraint` built from a `VALID_DOC_TYPES = ("insurance_certificate", "license")` module constant — same `VALID_STATUSES`-style pattern `Estimate`/`ChangeOrder` already use), `storage_path` (`Text`, not null), `expires_on` (`Date`, not null — a plain calendar date, not a timestamp; the schema doc's own `DATE` type, no time-of-day component), `created_at` (`TimestampMixin` only, no `updated_at` — no update route exists for this table at all, matching `Esignature`'s "immutable from creation" precedent). No `UpdatedAtMixin`.
- `SubcontractorAssignment`: `id`, `project_id` (FK, `ondelete="CASCADE"`), `subcontractor_id` (FK, no ondelete), `company_id` (FK, no ondelete), `assigned_by` (FK to `users.id`, no ondelete, not null), `override_reason` (`Text`, nullable — populated only when the assignment overrides an expired-compliance block), `created_at` (`TimestampMixin` only).
- `ComplianceNotification`: `id`, `company_id` (FK, no ondelete), `compliance_document_id` (FK, `ondelete="CASCADE"`), `threshold` (`String(10)`, not null, `CheckConstraint` from `VALID_THRESHOLDS = ("30_day", "14_day", "7_day")`), `fired_at` (`TimestampMixin`, aliased/used as the "when this notification fired" timestamp — do not add a second, separately-named timestamp column; `TimestampMixin`'s own `created_at` IS `fired_at` semantically, so name the mapped attribute `fired_at` directly rather than adding `TimestampMixin` and a redundant second column — see Step detail below), `read_at` (`DateTime(timezone=True)`, nullable, no default — `None` until dismissed). `__table_args__` also declares a `UniqueConstraint("compliance_document_id", "threshold", name="uq_compliance_notifications_document_threshold")` — this is the actual duplicate-notification guarantee (Section 1 of the design spec), not application-layer discipline alone.
  - Judgment call, pre-resolved: do NOT use `TimestampMixin` for `ComplianceNotification` (which would add a column literally named `created_at`) — instead declare `fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)` directly (reusing the `utcnow` helper from `app/models/base.py`), so the column name matches its actual semantic meaning (`fired_at`, not `created_at`) the way `Esignature.signed_at` already does for the identical "this mixin's generic name doesn't fit, roll your own column" reason.
- Register all four in `app/models/__init__.py`'s import list and `__all__`, after `ChangeOrder` (creation order).
- Tests: none yet (models are exercised by Task 3.2's migration verification and every later task's own tests) — matches every prior phase's own "models task has no dedicated test file, migration/router tasks cover it" precedent (e.g. Task 2.7/2.20).
- Verify: `python -c "from app.models import Subcontractor, ComplianceDocument, SubcontractorAssignment, ComplianceNotification"` imports cleanly with no circular-import errors.
- Commit: `feat: add Subcontractor, ComplianceDocument, SubcontractorAssignment, and ComplianceNotification models`

## Task 3.2: Compliance Tracking Migration (0009)

**Files:** Create `backend/migrations/versions/0009_compliance_tracking_schema.py`.

- `down_revision = "0008"` (after Change Orders — this is genuinely the next migration in sequence, no re-sequencing note needed unlike Task 2.8's `esignatures`-before-`estimates` correction).
- Create all four tables in FK-dependency order: `subcontractors` (references `companies`) → `compliance_documents` (references `subcontractors`, `companies`) → `subcontractor_assignments` (references `projects`, `subcontractors`, `companies`, `users`) → `compliance_notifications` (references `companies`, `compliance_documents`).
- Each table's own `CheckConstraint`/`UniqueConstraint` mirrors what Task 3.1's ORM models declare — same "migration and model both declare the same DB-level constraint, belt-and-suspenders" pattern every prior migration uses.
- RLS: all four tables get the ordinary, single, non-inherited `tenant_isolation` policy shape — guarded-cast `NULLIF(current_setting('app.current_tenant', true), '')::uuid`, routed through `get_all_descendant_ids()`, `FOR ALL` with matching `USING`/`WITH CHECK` — copy migration `0008`'s exact block structure (`for table in (...): op.execute("ALTER TABLE ... ENABLE ROW LEVEL SECURITY") ...`) for all four tables in one loop.
- No `REVOKE` on `subcontractors`/`subcontractor_assignments`/`compliance_notifications` (all three are legitimately mutable — a subcontractor's `name`/`trade`/`contact_email` might need correction, a `subcontractor_assignment` has no update route planned but isn't declared immutable by the spec either way so default to the ordinary app_user grants, and `compliance_notifications.read_at` is explicitly written by the dismiss route). **`compliance_documents` DOES get the immutable-table `REVOKE UPDATE, DELETE` treatment** (matching `esignatures`/`daily_logs`/`documents`'s own precedent) — no update/delete route is ever planned for it (Task 3.1's own docstring rationale), and Section 4 of the design spec explicitly calls this out as "immutability by omission."
- Verify by hand against live Postgres: apply the migration, confirm `\d subcontractors`, `\d compliance_documents`, `\d subcontractor_assignments`, `\d compliance_notifications` show the expected columns/constraints; confirm `compliance_documents` genuinely rejects a raw `UPDATE`/`DELETE` as `app_user` (same empirical check every REVOKE'd table's own migration task has performed).
- Run the full suite (still green — no other test file should be affected by a purely additive migration).
- Commit: `feat: add Compliance Tracking migration (subcontractors, compliance_documents, subcontractor_assignments, compliance_notifications)`

## Task 3.3: Subcontractor & ComplianceDocument Schemas

**Files:** Create `backend/app/schemas/subcontractor.py`, `backend/app/schemas/compliance_document.py`.

- `SubcontractorCreateRequest`: `name` (required), `trade` (optional), `contact_email` (optional, `EmailStr | None`).
- `SubcontractorResponse`: full model (`id`, `company_id`, `name`, `trade`, `contact_email`, `created_at`), `ConfigDict(from_attributes=True)`.
- `SubcontractorListResponse`: cursor-paginated envelope, matching `MarkupProfileListResponse`'s/every other list schema's exact shape.
- `ComplianceDocumentCreateRequest`: this is the JSON-body-shaped part of a multipart request (`doc_type`, `expires_on` — the actual file itself arrives as a separate `UploadFile` form field in the router, matching `DocumentCreateRequest`'s own split between a Pydantic schema for the non-file fields and a router-level `File(...)` parameter for the binary — check `app/routers/projects.py`'s `upload_document` route for the exact precedent to mirror, since Documents already established "no single Pydantic schema models the multipart body as a whole, the router destructures `Form(...)`/`File(...)` parameters directly" — for `ComplianceDocumentCreateRequest`, since there's no pre-existing exact analog with two Form fields, model `doc_type`/`expires_on` as plain router-level `Form(...)` parameters too, NOT a Pydantic request schema, for consistency with the multipart-body convention already established; do not create `ComplianceDocumentCreateRequest` as a schema class at all — this bullet exists to document why not).
- `ComplianceDocumentResponse`: full model (`id`, `subcontractor_id`, `company_id`, `doc_type`, `storage_path`, `expires_on`, `created_at`), `ConfigDict(from_attributes=True)`.
- `ComplianceDocumentListResponse`: cursor-paginated envelope.
- Tests: none yet (schemas are exercised by Task 3.4/3.5's own router tests).
- Verify: `python -c "from app.schemas.subcontractor import SubcontractorCreateRequest, SubcontractorResponse, SubcontractorListResponse; from app.schemas.compliance_document import ComplianceDocumentResponse, ComplianceDocumentListResponse"` imports cleanly.
- Commit: `feat: add Subcontractor and ComplianceDocument schemas`

## Task 3.4: POST/GET /subcontractors, GET /subcontractors/{id}

**Files:** Create `backend/app/routers/subcontractors.py`. Modify `backend/app/main.py`. Create `backend/tests/test_subcontractors.py`.

- New router file (not `projects.py` or any existing file — `Subcontractor` is a standalone, company-scoped resource with no project/lead parent, closer in shape to `MarkupProfile`/`CostCatalogItem` than to any project-nested resource), `router = APIRouter(prefix="/subcontractors", tags=["subcontractors"])`.
- `_WRITE_ROLES = ("admin",)` — per the RBAC matrix (`docs/07-security-compliance.md` Section 2's "Compliance" row: Admin = Full CRUD, Project Manager = Read + assign only, no create). `_READ_ROLES = ("admin", "project_manager", "accountant")`.
- `POST /subcontractors`: `require_role(*_WRITE_ROLES)`, creates with `company_id=current.company_id` (a standalone top-level resource — no parent entity's own `company_id` to defer to, matching `create_project`'s/`create_lead`'s own precedent, NOT the nested-resource pattern Tasks 2.21-2.23's `company_id` fix applies to).
- `GET /subcontractors`: `require_role(*_READ_ROLES)`, paginated list, no status/role-based row-scoping needed (unlike Estimates' `client` status-scoping — there is no `client`-role access to this resource at all).
- `GET /subcontractors/{id}`: `require_role(*_READ_ROLES)`, `_get_subcontractor_or_404` helper (mirrors `_get_estimate_or_404`'s exact shape — RLS makes cross-tenant invisible, 404 covers both "doesn't exist" and "exists but isn't yours" identically).
- Register `subcontractors.router` in `app/main.py`, after `change_orders.router`.
- Tests: create (admin succeeds, PM/accountant/field_crew/client 403), list (all three read roles succeed, field_crew/client 403), get-by-id (success + cross-tenant 404 + nonexistent 404), pagination.
- Verify: full suite green.
- Commit: `feat: add Subcontractor creation and listing`

## Task 3.5: POST/GET /subcontractors/{id}/compliance-documents

**Files:** Modify `backend/app/routers/subcontractors.py`, `backend/app/services/document_storage.py`. Modify `backend/tests/test_subcontractors.py`.

- New `write_compliance_document_file(*, company_id: uuid.UUID, subcontractor_id: uuid.UUID, compliance_document_id: uuid.UUID, content: bytes) -> str` in `document_storage.py`, writing to `{storage_root}/{company_id}/subcontractors/{subcontractor_id}/{compliance_document_id}.{ext}` — mirrors `write_estimate_pdf_file`'s/`write_esignature_artifact_file`'s own "differs from `write_document_file` in N explained ways" docstring convention. Exclusive-create (`"xb"` mode, never overwrite — `compliance_documents` has no update route, matching the model's own immutability). No `validate_file_name` call (the filename is system-generated from `compliance_document_id`, not user-controlled — same reasoning `write_estimate_pdf_file`/`write_esignature_artifact_file` already give for skipping that check).
  - Judgment call, pre-resolved: file extension is derived from `doc_type`-independent, generic content — do NOT attempt to infer/validate a specific extension from the uploaded file's content-type (no established precedent in this codebase validates upload MIME types beyond what FastAPI's own `UploadFile` already exposes); store with a fixed `.bin` extension if the original filename/content-type isn't meaningfully checkable, OR preserve the caller-supplied filename's own extension via `Path(original_filename).suffix` with no further validation (prefer this second option — it keeps the stored file openable by a human without a rename, and `Document`'s own `file_name` column already establishes "trust the caller's filename for a legitimate internal-use upload" as an acceptable precedent for Admin-only upload routes).
- `POST /subcontractors/{id}/compliance-documents`: `require_role(*_WRITE_ROLES)` (admin only), multipart form (`doc_type: str = Form(...)`, `expires_on: date = Form(...)`, `file: UploadFile = File(...)`) — validate `doc_type` against `VALID_DOC_TYPES` in Python before any file write (422 if invalid, mirroring `capture_esignature`'s own Task 2.18 "validate before any side effect" fix), `_get_subcontractor_or_404` first.
- `GET /subcontractors/{id}/compliance-documents`: `require_role(*_READ_ROLES)`, paginated list scoped to `subcontractor_id`.
- Tests: create (admin succeeds with a real file round-trip, PM/accountant/field_crew/client 403, invalid `doc_type` 422 with no orphaned file, cross-tenant subcontractor_id 404), list (all three read roles, pagination, cross-tenant 404).
- Verify: full suite green.
- Commit: `feat: add compliance document upload and listing`

## Task 3.6: GET /compliance/dashboard

**Files:** Create `backend/app/routers/compliance.py`. Modify `backend/app/main.py`. Create `backend/tests/test_compliance_dashboard.py`.

- New router file (`Compliance` spans multiple entities — `Subcontractor`, `ComplianceDocument`, `ComplianceNotification` — none of which it "nests under" the way project-scoped resources do; matches `catalogs.py`'s own precedent of a router organized around a cross-cutting concept, not a single parent entity), `router = APIRouter(prefix="/compliance", tags=["compliance"])`.
- `GET /compliance/dashboard`: `require_role("admin", "project_manager", "accountant")`. Computes, live, from `compliance_documents` joined to `subcontractors` (for `name`/`trade` display fields): every row where `expires_on <= today + 30 days` (both "expiring soon" and "already expired" — a single query, the response schema's own `status` field, computed in Python from comparing `expires_on` to today, distinguishes `"expiring_soon"` vs `"expired"` for display, not two separate DB queries). No pagination (a company-wide compliance dashboard is expected to be a bounded, glanceable list — if this becomes a real scale problem later, that's a future task, not a YAGNI violation now to pre-optimize for).
- `ComplianceDashboardEntry` schema (new, in `app/schemas/compliance.py`): `compliance_document_id`, `subcontractor_id`, `subcontractor_name`, `doc_type`, `expires_on`, `status` (`"expiring_soon" | "expired"`, computed).
- `ComplianceDashboardResponse`: `items: list[ComplianceDashboardEntry]`.
- Register `compliance.router` in `app/main.py`, after `subcontractors.router`.
- Tests: a compliance document expiring in 25 days appears with `status="expiring_soon"`; one expired 5 days ago appears with `status="expired"`; one not expiring for 60 days does NOT appear at all; RBAC (field_crew/client 403); cross-tenant isolation (only the caller's own company's expiring/expired docs appear, not another tenant's).
- Verify: full suite green.
- Commit: `feat: add compliance dashboard, computed live from document expiry dates`

## Task 3.7: Compliance Tenant-Isolation Regression Tests (Subcontractors/Documents)

**Files:** Extend `backend/tests/test_tenant_isolation_phase2.py` (or start a `test_tenant_isolation_phase3.py` — use judgment; given this is the first Phase 3 task needing dedicated isolation coverage and Phase 2's own file is already large (~1500+ lines after the Task 2.24/follow-up additions), starting a new `test_tenant_isolation_phase3.py` file is the better call here, matching how `test_tenant_isolation_phase1.py`/`test_tenant_isolation_phase2.py` themselves each started fresh rather than endlessly extending `test_tenant_isolation.py`).

- Same rigor as Tasks 2.6/2.16/2.24: cross-tenant 404, header-spoofing blocked, for `subcontractors` and `compliance_documents`. One RLS-disable/re-enable proof (on `subcontractors`, representative of the plain policy shape all four new tables share — matching the "prove the mechanism once per distinct shape" precedent).
- Include the parent/child hierarchy visibility case (a parent-company admin can see a child branch's Subcontractors/ComplianceDocuments, siblings cannot see each other's) — same precedent as every prior phase's own isolation file.
- **Explicitly apply the company_id-sourcing lesson from the post-Phase-2 follow-up fix**: `POST /subcontractors/{id}/compliance-documents` nests under a Subcontractor the same way `create_change_order` nests under a Project — verify `company_id` on the created `ComplianceDocument` row is derived from `subcontractor.company_id`, not `current.company_id`, using the exact same parent/descendant-branch empirical test shape already established in `test_tenant_isolation_phase2.py`'s own `test_creating_change_order_under_child_branch_project_uses_project_company_id`. This is a NEW test proving the router task (3.5) got this right from the start, not a bug being fixed after the fact.
- Verify: full suite green, run twice.
- Commit: `test: add tenant-isolation regression coverage for Subcontractors and ComplianceDocuments`

## Task 3.8: check_compliance_expiry Scheduler Actor

**Files:** Create `backend/app/tasks/compliance_expiry.py`. Modify `backend/app/tasks/broker.py` docstring only if it needs updating (check first — it likely doesn't, since the broker itself doesn't change). Create `backend/tests/test_compliance_expiry_task.py`.

- `_check_compliance_expiry(session_factory) -> None` (or however this codebase's established async-actor pattern threads a session — check `app/tasks/estimate_pdf.py`'s own `_generate_estimate_pdf` for the exact "how does a worker actor get its own AsyncSession and set tenant context" pattern first, since Task 2.15 already solved the "RLS chicken-and-egg bootstrap for worker code" problem once; **do not re-solve it, reuse it**): iterates every company (a worker action has no single caller-scoped tenant context the way an HTTP request does — needs its own cross-tenant query pattern, likely as the OWNER/superuser connection rather than a tenant-scoped one, since a daily company-wide scan is legitimately a cross-tenant operation unlike anything else in this codebase so far — this is a genuinely new pattern, not a precedent-following one; use `current_setting`-bypassing direct queries, matching how `tests/conftest.py`'s own owner-connection helpers work, but from application code rather than test code — flag this clearly in the implementation's own docstring as "the one place in this codebase that legitimately queries across all tenants at once, and why").
- For each `compliance_documents` row: compute `days_until_expiry = (expires_on - today).days`. For each of `30, 14, 7`: if `days_until_expiry <= threshold` AND no existing `compliance_notifications` row for `(compliance_document_id, threshold)`, insert one. (A document already 25 days from expiry on the FIRST run this logic ever executes against it correctly fires BOTH the `30_day` and, if within 14 days, the `14_day` notification in the same run — there is no requirement that thresholds fire in strict daily-decreasing order, only that each fires at most once ever.)
- `_check_compliance_expiry` is the plain, undecorated async function tests call directly. `check_compliance_expiry = dramatiq.actor(max_retries=3, actor_name="check_compliance_expiry")(_check_compliance_expiry)` is the thin wrapper the scheduler's `.send()` call uses — same split Task 2.15 established for `_generate_estimate_pdf`/`generate_estimate_pdf`.
- Tests: single company, single document, single threshold crossing → one notification row created. Same document, second run → no duplicate (the `UniqueConstraint` from Task 3.1 is exercised, not just trusted). Multiple companies, no cross-tenant notification leakage (Company A's expiring document never produces a `compliance_notifications` row with Company B's `company_id`). A document expiring in 25 days fires exactly the `30_day` notification, not `14_day`/`7_day`. A document that already has its `30_day` notification but is now within the `14_day` window fires exactly the new `14_day` one on this run, leaving the existing `30_day` row untouched.
- Verify: full suite green.
- Commit: `feat: add check_compliance_expiry background job`

## Task 3.9: Scheduler Service (APScheduler) + Docker Compose Wiring

**Files:** Create `backend/app/scheduler.py`. Create `backend/Dockerfile.scheduler` (or reuse `backend/Dockerfile` with a different `command:` in Compose — use judgment; given the scheduler needs `apscheduler` installed but NOT the full `backend`/`worker` runtime surface, a separate minimal Dockerfile is cleaner, but reusing the same image with a different pip-extras group is less duplication — resolve by adding `apscheduler` to the main `pyproject.toml`'s core dependencies rather than a separate extras group, since it's a single small library and this codebase hasn't established an extras-group convention anywhere yet; reuse the existing `backend/Dockerfile` and just point Compose's new `scheduler` service at it with a different `command:`). Modify `backend/pyproject.toml`. Modify `docker-compose.yml`.

- `app/scheduler.py`: a single `if __name__ == "__main__":` entrypoint using `apscheduler.schedulers.blocking.BlockingScheduler`, one daily job (`trigger="cron", hour=2` — run at 2am server time, an arbitrary but reasonable off-peak default; do not make this configurable via an env var in this task, that's unnecessary scope for a solo-dev project with no fixed deadline) that calls `check_compliance_expiry.send()` (importing the Dramatiq actor from Task 3.8, NOT re-implementing the expiry logic in the scheduler process itself — the scheduler's only job is enqueueing).
- `docker-compose.yml`: new `scheduler` service, same `build: ./backend` as `worker`, same `env_file: .env`, `depends_on: {postgres: service_healthy, redis: service_healthy}`, `command: python -m app.scheduler`, no `ports:` (not HTTP-reachable, matching `worker`'s own shape).
- Add `apscheduler>=3.10,<4.0` to `backend/pyproject.toml`'s core dependencies.
- Verify by hand: bring up the full local stack including the new `scheduler` service (`docker compose up -d --build`), confirm the container starts and stays running (doesn't crash-loop) without needing to wait a full day to observe a real firing — temporarily point the cron trigger at "run once, 10 seconds from now" via a manual local test (not committed), observe a `check_compliance_expiry` message actually reach the `worker` service's logs, then revert to the real daily schedule before committing.
- Run the full suite (still green — nothing in `pytest` exercises the scheduler process itself, only `_check_compliance_expiry` directly per Task 3.8's own tests).
- Commit: `feat: add scheduler service for daily compliance expiry checks`

## Task 3.10: GET/POST /compliance/notifications

**Files:** Modify `backend/app/routers/compliance.py`, `backend/app/schemas/compliance.py`. Modify `backend/tests/test_compliance_dashboard.py` (or split into a new `test_compliance_notifications.py` — use judgment based on how large the dashboard test file has grown by this point).

- `GET /compliance/notifications`: `require_role("admin")` only (per the design spec's Section 2 — US-7.1 specifies notifications go "to the Admin" specifically). Paginated list of `ComplianceNotification` rows for the caller's company, joined to `compliance_documents`/`subcontractors` for display context (`subcontractor_name`, `doc_type`, `expires_on`, `threshold`, `fired_at`, `read_at`). Optionally filterable by `?unread_only=true` (a `read_at IS NULL` filter) — include this, it's a natural, cheap addition to a notification list, not scope creep (every real notification UI needs it, and it's a single `WHERE` clause).
- `POST /compliance/notifications/{id}/dismiss`: `require_role("admin")` only, sets `read_at = utcnow()` if not already set (idempotent — dismissing an already-dismissed notification is a 200 no-op, not a 409; there's no legitimate reason to treat double-dismissal as an error condition here).
- Tests: list (admin only, PM/accountant/field_crew/client all 403 — this is the one Compliance route where even `project_manager`/`accountant` are excluded, unlike every other read route in this feature), `unread_only` filtering, dismiss (success, idempotent double-dismiss, cross-tenant 404, admin-only RBAC).
- Verify: full suite green.
- Commit: `feat: add compliance notification listing and dismissal`

## Task 3.11: POST/GET /projects/{id}/subcontractor-assignments

**Files:** Create `backend/app/routers/subcontractor_assignments.py`. Modify `backend/app/main.py`, `backend/app/schemas/subcontractor.py` (or a new `app/schemas/subcontractor_assignment.py` — use judgment; given this schema is genuinely about the assignment relationship, not the Subcontractor entity itself, a dedicated file matches this codebase's established "one schema file per resource" convention better). Create `backend/tests/test_subcontractor_assignments.py`.

- New router file — nests under `/projects/{project_id}/subcontractor-assignments`, mirroring `change_orders.py`'s own precedent for a resource that's conceptually project-nested but got its own file (this router will likely stay small, unlike `change_orders.py`, but matching the "resource with its own distinct business rule, own file" precedent is still the right call here since the override-logic is substantial enough to warrant its own docstring space without bloating `projects.py`).
- `SubcontractorAssignmentCreateRequest`: `subcontractor_id`, `override_reason: str | None = None`.
- `SubcontractorAssignmentResponse`: full model.
- `POST /projects/{project_id}/subcontractor-assignments`: reachable by `require_role("admin", "project_manager")`. `_get_project_or_404` first. Query for any `compliance_documents` row belonging to `subcontractor_id` with `doc_type` in `VALID_DOC_TYPES` and `expires_on < today` (an EXPIRED document specifically — absent documents do NOT block, per the design spec's explicitly-resolved judgment call). If any expired document exists:
  - `current.role == "project_manager"` → 409, regardless of whether `override_reason` was supplied.
  - `current.role == "admin"` → require `override_reason` non-empty (422 if blank/omitted), write an `audit_log` entry (`action="subcontractor.assigned_with_expired_docs"`, `entity_type="subcontractor_assignment"`, `metadata={"reason": override_reason, "subcontractor_id": ...}`, `company_id=project.company_id` — per the company_id-sourcing lesson from the post-Phase-2 follow-up, NOT `current.company_id`).
  - If no expired document exists, either role may assign freely, `override_reason` is ignored/not required even if supplied.
  - `company_id=project.company_id` on the `SubcontractorAssignment` row itself too (same lesson, applied from the start rather than fixed later).
- `GET /projects/{project_id}/subcontractor-assignments`: `require_role("admin", "project_manager", "accountant")`, paginated list scoped to `project_id`.
- Register `subcontractor_assignments.router` in `app/main.py`.
- Tests: all four PM/Admin × expired/compliant combinations from the design spec's own Section 5 testing note; audit log content verification (raw SQL, not just the route's own response) for the override case; absent-documents-does-not-block case explicitly (a subcontractor with zero `compliance_documents` rows assigns freely by either role, no `override_reason` needed); cross-tenant 404 on both routes; the company_id-sourcing empirical test (parent-company session assigning against a child-branch Project without switching `X-Tenant-ID`, confirming the resulting `SubcontractorAssignment.company_id` and the audit log's `company_id` both match the child, not the parent — proactively covering the exact bug class the post-Phase-2 follow-up fixed, from day one this time).
- Verify: full suite green.
- Commit: `feat: add Subcontractor assignment with Admin-override-required expired-compliance rule`

## Task 3.12: Compliance Tenant-Isolation Regression Tests (Notifications/Assignments)

**Files:** Extend `backend/tests/test_tenant_isolation_phase3.py` (started in Task 3.7).

- Same rigor as Task 3.7, now for `compliance_notifications` and `subcontractor_assignments`: cross-tenant 404, header-spoofing blocked. No second RLS-disable/re-enable proof needed (Task 3.7's proof on `subcontractors` already covers this shared policy shape) — note this explicitly in the section's own comment banner, matching the "prove the mechanism once per distinct shape" precedent's own self-documentation style established in `test_tenant_isolation_phase2.py`.
- Parent/child hierarchy visibility for `subcontractor_assignments` (parent admin sees a child branch's assignments, siblings cannot see each other's).
- Verify: full suite green, run twice.
- Commit: `test: add tenant-isolation regression coverage for ComplianceNotifications and SubcontractorAssignments`

## Task 3.13: Full-Stack E2E Extension

**Files:** Modify `scripts/e2e_smoke_test.py`.

- Extend the existing live-stack smoke test with the Compliance Tracking exit criterion, over real HTTP against real containers: create a Subcontractor, upload a compliance document with an `expires_on` date within the 30-day window (a hand-verifiable, deterministic date — e.g. `today + 20 days`, computed in the script itself, not hardcoded to a literal calendar date that would go stale), assert `GET /compliance/dashboard` shows it with `status="expiring_soon"`, attempt a `POST /projects/{id}/subcontractor-assignments` as a `project_manager` against a subcontractor with a separately-uploaded ALREADY-EXPIRED document and assert 409, then retry as `admin` with an `override_reason` and assert success plus an audit log entry.
- Do NOT attempt to exercise the scheduler service / `check_compliance_expiry`'s actual daily firing in this script — same reasoning Task 2.25 gave for deferring the `worker`/PDF-export leg: reliably waiting on a real cron-scheduled background job inside a smoke-test script is disproportionately flaky for what this test needs to prove, and `test_compliance_expiry_task.py` (Task 3.8) already exercises that logic directly and thoroughly. Note this deferral explicitly in the script's own docstring, matching Task 2.25's own precedent exactly.
- Run against the full local stack per the established procedure, now including the `scheduler` service in the `docker compose up -d --build` invocation (even though this script doesn't directly test it, it should still start cleanly alongside everything else — confirm this by hand as part of this task's own verification, not skip it).
- Commit: `test: extend E2E smoke test with Compliance Tracking flow (Phase 3 Compliance exit criterion)`

## Task 3.14: Full Regression Pass + Plan Closeout

**Files:** None (verification only) — update the design spec's own exit-criteria section (add one to `docs/superpowers/specs/2026-07-13-compliance-tracking-design.md` if it doesn't already have a checklist, or add a short "Implementation Status" note at the top referencing this plan's completion).

- Full `pytest -v` from `backend/`, run twice for stability.
- Full RLS regression suite (`test_tenant_isolation.py` + `test_rls_policy_regression.py` + `test_tenant_isolation_phase1.py` + `test_tenant_isolation_phase2.py` + `test_tenant_isolation_phase3.py`) — confirm no regression in any earlier phase's own tables from anything this plan touched.
- CI green on a real GitHub Actions run (same discipline as every prior phase's own closeout task — open a PR, confirm the run; merging is the user's own explicit decision, not automatic).
- Commit: `docs: close out Compliance Tracking implementation`
