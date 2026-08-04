# The field-crew offline write queue — design

The other half of PRD §8's offline question. `2026-08-02-offline-pwa-design.md`
§2 scoped it and then deliberately set it aside: it is a *separate, much
smaller* feature that shares almost nothing with estimator capture beyond
the words "offline support," and it should be decided on its own merits
rather than bundled.

This is that decision. It is written after estimator capture shipped
(PR #133), which changes the cost: the service worker, the identity-keyed
IndexedDB store, the reachability signal and the defer/park flush rules all
exist. What remains is genuinely small — and two things in it are *not*
small, which is what this document is for.

---

## 1. What a field crew member can actually do

Derived from `require_role(...)`, not from the product description. A
`field_crew` user can perform exactly **two writes in the entire product**:

| Route | Shape | Scope |
|---|---|---|
| `PATCH /tasks/{id}` | `status` **only** | A task assigned to them (404 otherwise — `_get_task_or_404` folds the assignee filter in, so someone else's task does not exist from their side) |
| `POST /projects/{id}/daily-logs` | Create | A project they hold an assigned task on (`get_project_or_404`'s field-crew scope) |

Everything else they can reach is a read. That is the entire surface, and
it is why this is days rather than weeks.

## 2. The two things that are not small

### 2.1 A duplicated daily log can never be removed

`daily_logs` is immutable **at the database level**: migration 0004 does
`REVOKE UPDATE, DELETE ON daily_logs, documents FROM app_user`, and no
update or delete route exists in any router. That is a deliberate property
of a site record — but it means a duplicate created by a replay is
**permanent**. Nobody can clean it up through the product; it needs the
table owner and a shell.

The parent spec called this "an idempotency key, not a merge strategy," and
that is right, but it understated the stakes: without a key, the failure is
not an untidy list, it is an unfixable one. A replay is not exotic either —
the classic case is the request arriving, the row committing, and the
response dying on the way back, which is precisely what a van driving out
of a coverage cell produces.

**Decision: `client_reference`, a client-generated UUID in the request
BODY, unique per company.** Migration 0034 adds the column and the index;
`POST /projects/{id}/daily-logs` looks it up first and returns the existing
row instead of creating a second one, so a replay is a no-op that still
answers 201 with the log the crew member wrote.

**In the body, not an `Idempotency-Key` header**, and that is not a style
preference: the Next BFF forwards a fixed header allowlist (`Content-Type`,
`Authorization`, `X-Tenant-ID`, `X-Forwarded-For`), so a header would be
silently dropped on the hop and every replay would create a duplicate with
the code *looking* correct. This is the same trap `app/services/concurrency.py`
documents for `expected_updated_at` versus `If-Match`, and the same answer.

Nullable, because every existing caller — the project screen's own daily-log
form, and every test — omits it. A log written online without one is exactly
what it is today.

### 2.2 A queued status change can land hours after it was made

`tasks` has **no `updated_at` column** (see `app/models/task.py`: "only
`created_at`... status/assignee changes are not tracked"), so
`expected_updated_at` is not available, and `PATCH /tasks/{id}` has no
stale-write guard at all today. Online that is defensible: last-write-wins
between two people clicking within seconds of each other.

A queue changes the *kind* of the problem, not just the odds. A crew member
marks a task done at 09:00 with no signal; a project manager moves it back
to `open` at 11:00 because the work was rejected; the queue flushes at 17:00
and silently sets it to `done` again. Nothing errors, and the PM's decision
is gone.

**Decision: `expected_status` — compare-and-set, in the body.** The client
sends the status it *saw*; the server refuses with **409** and reports the
current value if the task has moved since. No migration, no new column, and
the existing `status` column is the version: for a three-value enum that
only moves when a human moves it, the value *is* the version.

This deliberately copies `expected_unit_rate` (PR #128) rather than
inventing a second dialect — same reasoning (a write that assumes a value it
read), same 409, same shape of remedy. Optional, so every existing caller is
unaffected.

The remedy differs from the estimator's, and that is the point of stating
it: there is nothing to "adopt." The crew member is shown what the task says
now and chooses to apply their change or drop it.

## 3. What gets cached, and where the crew works

**`/my-tasks`**, which is already the field crew's whole product: a
cross-project list of their assigned tasks. It becomes the second entry in
the worker's `OFFLINE_PATHS`, and it grows an offline panel identical in
behaviour to the capture screen's — "Make available offline", what is stored
and when, "Remove offline data".

Cached, keyed by `(user_id, active_company_id)` exactly as before:

| Data | Why |
|---|---|
| The crew member's assigned tasks | The list they act on, and the statuses they compare against |
| The projects those tasks belong to | `POST /daily-logs` needs a real project id, and `MyTaskResponse` already carries `project_id`/`project_name` — no extra request |

**Daily logs are written from `/my-tasks`**, against the projects the
crew member's own tasks name. That is not a shortcut around the backend's
scope, it *is* the backend's scope: `get_project_or_404` admits a field-crew
caller only to a project they hold an assigned task on, so the set of
projects reachable from their task list is exactly the set they may log
against. Offering anything wider would produce a 404 hours later.

Nothing here is commercially sensitive the way the cost catalog is — a
task list is not a price list — but it is still tenant data outside RLS, so
the key and the clear-on-logout/switch rules are unchanged.

## 4. What defers, what parks, what is never retried

Same three outcomes as the estimator flush, and the same rule: **the queue
never resolves anything a human should**.

| Outcome | When | What happens |
|---|---|---|
| **deferred** | The request did not arrive | Stays queued, retried on the next tick. The ordinary case for a device with no signal |
| **parked** | 409 — the task moved underneath them | Surfaced with what it says now, and never retried automatically |
| **parked** | 403 (subscription lapsed), 404 (task reassigned, project gone) | Surfaced, never retried — it will not start succeeding |
| **sent** | 201/200 | Removed from the queue |

A 404 deserves its own note: a crew member unassigned while they were
offline gets one, and it is the correct answer — the row is genuinely
invisible to them now. The queue must say so rather than retrying into
silence.

## 5. What this deliberately does not include

- **Photos on daily logs.** `daily_logs` has no document linkage at all
  (Phase 1 shipped it text-only, deliberately); queuing a file upload is a
  different feature with a different storage story.
- **Offline project detail, documents, or any other screen.** The crew's two
  writes both fit on `/my-tasks`.
- **Creating tasks offline.** A field crew member cannot create a task
  online either.
- **A shared "queue" abstraction across both offline features.** The
  estimator's draft is one logical unit flushed as a three-call chain; these
  are two independent single-call writes. Forcing one shape over both would
  produce the four-boolean helper the codebase's own list-loader docstring
  warns about. The *storage*, *identity*, *reachability* and *retry* pieces
  are shared; the flush is not.

---

## Sources

Code as of `101e88b`: `app/routers/tasks.py` (`_get_task_or_404`,
`patch_task`, `list_my_tasks`), `app/routers/projects.py`
(`create_daily_log`), `app/models/task.py`, `app/models/daily_log.py`,
`app/schemas/daily_log.py`, `app/services/concurrency.py`, migration 0004's
REVOKE, `frontend/app/(app)/my-tasks/page.tsx`, `frontend/public/sw.js`,
`frontend/lib/offline/*`, and `2026-08-02-offline-pwa-design.md` §2.
