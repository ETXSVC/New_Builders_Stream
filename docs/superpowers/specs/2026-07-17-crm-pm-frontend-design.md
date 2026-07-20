# CRM + Project Management Frontend — Design Spec

> **Implementation Status (2026-07-19):** Implemented in full on `feature/crm-pm-frontend` — all six backend additions (Decision 2), every route/screen in Decision 3, the BFF handlers, and the role-aware nav. Frontend lint passes with 0 errors (the 3 pre-existing marketing `<img>` warnings remain) and `next build` compiles clean. Full backend regression suite green. Both Playwright specs pass live against the Docker Compose stack from a cold `next dev` server — `foundation.spec.ts` (updated for the optional-MFA landing and the real dashboard) and the new `crm-pm.spec.ts` covering the full lead→won→drafted-project→documents→daily-logs arc including the document content round-trip.
> **Beyond the approved spec:** one endpoint was added during planning — `GET /companies/members` (admin/PM), because the Phases & Tasks assignee picker and NewTaskRow had no way to list the company's users (no such read endpoint existed; discovered as a spec gap while writing Task 16's code, implemented as plan Task 10.5 with the same role/tenant conventions as the other Decision 2 additions).
> **Consolidated review outcome:** an independent review of the screen layer surfaced two Important fixes folded in before E2E — the Communications/Documents/Daily-logs lists now follow `next_cursor` to exhaustion (the backend pages ascending at 25/page, so a just-created entry lands on the last page), and the Nav/app-shell now mounts for every authenticated screen via `AppShell` (previously only the dashboard had navigation/logout, leaving field_crew and client stranded after their role redirects).
> **Deferred, unchanged from Out of scope:** invitation/user-management UI (so field_crew/client role journeys remain backend-tested + screen-built but not E2E-driven), company switcher, estimates tab, daily-log photos.

**Date:** 2026-07-17
**Depends on:** Frontend Foundation (PR #15, merged to `main`). Builds directly on Foundation's BFF session architecture, UI primitives, app shell, and generated API types. PRs #16 (MFA-optional redirect) and #17 (AuthContext hardening + register rate limit) are open but touch disjoint files; this sub-project branches from `main` and does not depend on either.
**Sub-project:** 2 of 6 in the frontend build-out (Foundation → **CRM+PM** → Estimation+E-Signature → Compliance+Billing → Invoicing+Reporting → Integrations+Admin).

## Goal

Replace Foundation's placeholder dashboard with real product screens for the two oldest, most complete backend feature areas: CRM (leads, communication logs) and Project Management (projects, phases, tasks, documents, daily logs). All four roles get a working surface: admin/project_manager (full CRUD), field_crew (a dedicated My Tasks view), and client (the sanitized project dashboard the backend already serves).

## Decision 1: Role scope — all four roles in this sub-project

Admin/PM get the full Leads + Projects screens. Field_crew gets a dedicated `/my-tasks` view (flat, cross-project list of their assigned tasks with one-tap status changes) rather than navigating the admin-shaped project screens with actions gated down — this matches how the backend already scopes their access and fits phone-on-a-job-site use. Client gets the sanitized project dashboard (`ProjectClientDashboardResponse`: name, status, site address, phase/task counts — the backend already shapes this response by role on the same endpoint, so the frontend renders conditionally on the response shape, not on a separate route).

Role knowledge on the client: the access token's JWT carries no role claim (verified — payload is `sub`/`default_company_id`/`iat`/`exp`/`jti` only), so the backend's `TokenResponse` gains a `role` field populated from the user's default membership at both mint sites (login and refresh — both already have the membership row in hand), exactly the pattern `mfa_enrollment_required` already uses. `AuthContext` stores it alongside the existing session state. The backend remains the sole authorization boundary; the frontend only uses role to decide what UI to render, and any 403 from the backend is handled as a normal error.

## Decision 2: Four small backend read-endpoint additions

The backend has write routes and (mostly) list routes for this domain, but four gaps block real screens. Each addition follows the existing router conventions (same role dependencies, tenant scoping, cursor pagination where applicable; reads are not `block_if_read_only`-gated, consistent with every existing GET):

1. **`GET /projects/{project_id}/documents/{document_id}/download`** — streams the file bytes from `storage_root/{company_id}/{project_id}/{version}/{file_name}` with a `Content-Disposition` attachment header. Same role/tenant checks as the existing document list route. Without this, the Documents tab could list files but never open one — no download endpoint exists anywhere today.
2. **`GET /dashboard/summary`** — returns counts via SQL COUNT: `{open_leads, active_projects, tasks_due_this_week}`. "Open leads" = status not in (won, lost). "Active projects" = status `active`. "Tasks due this week" = status not `done`, due_date within the next 7 days. Roles: admin/PM only — the only roles that see the dashboard (field_crew and client are redirected off it before this endpoint is ever called, and field_crew has no lead access anyway). Existing list endpoints are cursor-paginated with no total-count field, so client-side counting would either over-fetch or under-count.
3. **`GET /projects/{project_id}/phases`** — phases ordered by `sequence`, each with its tasks nested. No list route exists today for phases or tasks (only POST/PATCH) — the Phases & Tasks tab is unbuildable without it.
4. **`GET /tasks?assignee=me`** — cross-project list of tasks assigned to the current user, joined with enough context to render "task name · project name · due date · status" rows (the response enriches `TaskResponse` with `project_id`/`project_name`, since tasks only reference their phase directly). Powers the My Tasks view. Roles: any staff role (admin/PM/field_crew); in practice field_crew's primary surface.
5. **`role` on `TokenResponse`** — not a new endpoint, but a response-field addition to login/refresh so the frontend knows which UI to render (see Decision 1).
6. **`client` added to `GET /projects`'s allowed roles** — verified gap: `client` can `GET /projects/{id}` (receiving the sanitized dashboard shape) but is excluded from the list route (`_LIST_ROLES`), so a client has no way to discover their project's ID. The list route gains `client`, returning the same sanitized per-project shape the detail route already produces for that role (name, status, site address, counts) — no internal fields leak.

After these land, `frontend/lib/api/types.ts` is regenerated from the live backend's `/openapi.json` (the existing `generate:api-types` script), so all new Route Handlers are typed against real schemas.

## Decision 3: Routes and information architecture

All new routes live in the existing `(app)` route group. `frontend/middleware.ts`'s matcher extends from `["/dashboard/:path*", "/account/:path*"]` to also cover `/leads/:path*`, `/projects/:path*`, and `/my-tasks/:path*`.

| Route | Roles | Content |
|---|---|---|
| `/dashboard` | admin/PM | Summary metric cards (open leads, active projects, tasks due this week) + recent-leads and active-projects link lists. Field_crew landing here is redirected client-side to `/my-tasks`. Client is redirected to their project's detail page when they have exactly one project (the common case), or shown a minimal list of their projects' dashboard cards when they have several (via `GET /projects`, which gains client access — Decision 2 item 6). |
| `/leads` | admin/PM | Filterable list (status filter), cursor-paginated, "New lead" action. |
| `/leads/[id]` | admin/PM | Detail: breadcrumb status pipeline (all statuses, current highlighted) + quick-action buttons for the legal next transitions only (state machine mirrored client-side for display; the backend remains authoritative and a 409 is surfaced as an error toast). Communication log timeline below, oldest-first per the backend's ordering, with a compact channel-select + text + Add form. Editable lead fields via an Edit form. |
| `/projects` | all staff roles | List of projects (backend already scopes field_crew to assigned projects). "New project" action for admin/PM. |
| `/projects/[id]` | all roles | Role-adaptive. Staff: header (name, status badge, site address) + status-transition actions (legal next transitions only, same pattern as leads; `completed` transition failures from pending change orders surface the backend's 409 detail) + four tabs: **Overview** (editable fields, lead link if `lead_id` set), **Phases & tasks** (accordion: phases as expandable sections, tasks rows inside with name/assignee/due/status; Add phase, Add task, inline task status+assignee editing), **Documents** (list of latest versions with download links, multipart upload form), **Daily logs** (chronological list, add form with date/weather/notes). Client role: the sanitized dashboard card (name, status, site address, phase/task/completed counts) — rendered from the backend's role-shaped response, no tabs. |
| `/my-tasks` | field_crew (usable by any staff) | Flat list of the current user's assigned tasks across projects, each row: task name · project name · due date · status, with a status cycle control (`open → in_progress → done` values from the backend's `VALID_STATUSES`). |

## Decision 4: BFF Route Handlers

One thin Next.js Route Handler per backend interaction, following Foundation's exact conventions (forward `Authorization` bearer, map `ApiError` → JSON + status, no client-side calls to the backend ever):

- `api/leads` (GET list, POST create), `api/leads/[id]` (GET, PATCH), `api/leads/[id]/communications` (GET, POST)
- `api/projects` (GET, POST), `api/projects/[id]` (GET, PATCH), `api/projects/[id]/status` (PATCH)
- `api/projects/[id]/phases` (GET, POST), `api/projects/[id]/tasks` (POST), `api/tasks/[id]` (PATCH), `api/my-tasks` (GET)
- `api/projects/[id]/documents` (GET, POST multipart pass-through), `api/projects/[id]/documents/[docId]/download` (GET, streams the backend response through — the browser hits the Next.js origin, never the backend directly, preserving the BFF boundary)
- `api/projects/[id]/daily-logs` (GET, POST)
- `api/dashboard/summary` (GET)

The document upload handler forwards the multipart body as-is (file + `file_name` form field); the download handler streams the backend's response body and forwards `Content-Disposition`/`Content-Type`.

## Decision 5: Components

New components live beside Foundation's existing structure:

- `components/leads/` — `LeadsList`, `LeadForm` (create + edit), `LeadStatusPipeline`, `CommunicationLog`
- `components/projects/` — `ProjectsList`, `ProjectForm`, `ProjectStatusActions`, `PhaseAccordion`, `TaskRow`, `DocumentsTab`, `DailyLogsTab`, `ClientProjectDashboard`
- `components/tasks/` — `MyTasksList`
- `components/dashboard/` — `SummaryCards`, `RecentList`
- Nav (`components/app-shell/Nav.tsx`) gains role-conditional links: Leads + Projects for admin/PM, Projects only for accountant (the backend already grants accountant read access to project lists/detail), My Tasks for field_crew, none extra for client.

Existing primitives (`Button`, `Input`, `Label`, `Card`) are reused; the only new primitives anticipated are a `Select` (needed by channel/status/assignee pickers) and a `Textarea` (notes fields), both built in the same hand-written shadcn style as Foundation's.

All screens are client components using the established `useAuth()` + `fetch("/api/...")` pattern with the same hardening conventions Foundation settled on (try/catch with a network-failure message, `submitting` re-entrancy guards, `disabled` states, `aria-live` error text).

## Decision 6: State machine mirroring, display-only

The lead pipeline (`new→contacted→estimating→qualified→won`, any non-terminal→`lost`) and project transitions (`draft→pre_construction→active→{suspended,completed}`, `suspended→{active,completed}`, `completed→archived`) are mirrored in a small frontend constants module purely to decide which action buttons to render. The backend's transition validation remains the only enforcement; a 409 (illegal transition, or completion blocked by pending change orders) is displayed verbatim from the backend's `detail`. When a lead is marked won, the UI surfaces the auto-drafted project (the LEAD_WON handler creates it with an empty `site_address`) with a prompt linking to the new draft project so a PM fills in the address.

## Decision 7: Error handling, read-only mode, empty states

- Backend 403s (role denied) and 409s (state machine, change-order block) render as inline error messages with the backend's detail string — never swallowed.
- Read-only mode (past-due subscription): write attempts return the backend's 402/423-style block; the frontend surfaces the error as returned. No preemptive client-side read-only detection in this sub-project (the frontend has no subscription-status source yet; that arrives with the Billing sub-project).
- Every list has an empty state with a create prompt (e.g. "No leads yet — create your first lead").

## Decision 8: Testing — Playwright E2E as proof-of-done

Extend the Foundation E2E suite with a second spec covering the full CRM→PM arc against the live Docker Compose stack:

1. Register a company (admin), create a lead, log a communication, walk the lead `new→contacted→estimating→qualified→won`, and assert the auto-drafted project appears in `/projects`.
2. Open the drafted project, set its site address, transition `draft→pre_construction→active`, add a phase and a task, upload a document and download it back (asserting content round-trips), add a daily log.
3. Assert the dashboard summary cards reflect the created data.

Field_crew and client role flows are covered by backend tests already; the E2E keeps to the admin arc to stay within one browser session (inviting a second user mid-E2E would require the invitation-acceptance UI, which is out of scope — see below).

## Out of scope (deliberate)

- **Invitation/user-management UI** — the backend's invitation flow has no frontend; without it, real field_crew/client logins can't be created through the UI. Field_crew and client screens in this sub-project are built and unit-verifiable via role-shaped API responses, but full E2E coverage of those roles waits for the Admin sub-project's user-management screens.
- **Company switcher** — still open from Foundation (no backend route lists a user's companies).
- **Estimates on the project page** — Estimation is sub-project 3; the project detail page ships without an Estimates tab and gains one later.
- **Daily-log photos** — no backend photo field exists (deferred in Phase 1); notes/weather only.
- **Task comments, notifications, Gantt/calendar views** — YAGNI for this pass.
