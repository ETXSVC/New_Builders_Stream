# Offline / PWA support for field crews — scoping

**Status: scoping only. No implementation is proposed here, and one is not
recommended until §7's question is answered.**

PRD §5.2 lists offline/PWA as out of scope for v1, and §8 question 1 —
*"Does field crew usage require offline-capable mobile/PWA support?"*, owner
Product — is still open. This document exists to make that question cheap to
answer, by establishing what offline would actually cost against this
codebase rather than against a generic Next.js app.

Two findings dominate everything below, and they point in opposite
directions:

- **The offline surface is remarkably small.** A `field_crew` user can
  perform exactly **two writes** in the entire product. Not "two screens" —
  two routes.
- **The security architecture actively fights a cached app shell.** The
  per-request CSP nonce and the 15-minute in-memory access token are both
  load-bearing, both deliberate, and both incompatible with the naive
  service-worker approach.

---

## 1. What a field crew can actually do

Derived from `require_role(...)` on every route, not from the RBAC matrix
doc.

### Writes — the complete list

| Route | Shape | Offline character |
|---|---|---|
| `PATCH /tasks/{id}` | `status` **only**, and only where `assignee_id == self` | Small enum, own row only |
| `POST /projects/{id}/daily-logs` | Create | **Append-only** |

That is the entire write surface. `_DAILY_LOG_WRITE_ROLES` in
`app/routers/projects.py` exists precisely because daily logs are "the only
place field_crew gets any write verb at all" — the router says so in a
comment rather than leaving it to be rediscovered.

### Reads

`GET /my-tasks` (the landing page — `app/(app)/dashboard/page.tsx` redirects
`field_crew` there), the projects list and detail, project documents, daily
logs, and phases/tasks. All are scoped further than the role suggests:
`with_field_crew_scope` restricts the projects list to assigned projects, and
`get_project_or_404` applies the same scope at every by-id chokepoint.

### Why this matters

Both writes are unusually well-suited to a queue:

- **Daily logs are immutable by construction.** No update or delete route
  exists, and migration 0004 additionally does
  `REVOKE UPDATE, DELETE ON daily_logs, documents FROM app_user` at the
  database level. A replayed create cannot
  conflict with anything — the only failure mode is a *duplicate*, which is
  an idempotency problem, not a merge problem.
- **Task status is a small enum on a row only that user may touch.** Two
  crew members cannot race each other on the same task, because each may
  only patch a task assigned to themselves.

A general offline sync layer solves conflict problems this surface does not
have. That is the strongest argument for scoping any eventual
implementation to a **write queue**, not a replication engine.

---

## 2. The blocker: a cached app shell cannot satisfy the CSP

This is the finding that should drive the decision.

`app/layout.tsx` sets `export const dynamic = "force-dynamic"`, and
`middleware.ts` builds the Content-Security-Policy per request around a
**fresh 16-byte nonce** that Next stamps into the inline bootstrap scripts it
emits. `frontend/CLAUDE.md` records why prerendering was given up for it:

> Prerendered HTML cannot carry a per-request nonce; it would ship a
> build-time value, the browser would compare it against the header's fresh
> one, and every page would fail to hydrate under its own policy —
> **silently**, because nothing in the app itself errors.

A service worker serving a cached HTML document is prerendering by another
name. The cached document carries the nonce from whenever it was cached; the
CSP header the browser evaluates against comes from... nothing, offline, or
a stale cached header. Either way the app's own bootstrap scripts are
blocked and the page renders as a blank shell **with no error surfaced**.

`e2e/security-headers.spec.ts` asserts that every script tag carries the
response's own nonce and that a real page load logs no CSP violation. Any
offline shell must keep those assertions passing, which rules out simply
relaxing the policy.

**Three ways out, none free:**

1. **Serve the offline shell from a route with its own static CSP** — a
   dedicated `/offline` entry point that does not use `'strict-dynamic'` +
   nonce, with its own hashed script allowlist. Smallest blast radius;
   means the offline experience is a *separate app*, not the product UI.
2. **Move to hash-based CSP for the bootstrap** — build-time hashes instead
   of per-request nonces. Restores prerendering and makes caching viable,
   but it is a change to the app's central security control, and
   `'strict-dynamic'` with a nonce is what currently makes `script-src`
   meaningful.
3. **Cache data, not documents.** The service worker caches only `/api/*`
   responses and queues writes; the HTML is always fetched live. Preserves
   the CSP exactly — and provides **no** benefit when the device is truly
   offline at page load, only when it goes offline mid-session.

Option 3 is the only one that changes nothing security-relevant, and it is
also the one that does not deliver what "offline support" usually means.
That trade is the substance of the decision.

---

## 3. Credentials expire in 15 minutes and cannot be renewed offline

`jwt_expire_minutes: int = 15`. The access token lives **in React state
only** (`AuthContext`), never in storage — a fresh tab starts with
`accessToken === null` and takes one round-trip to recover. The refresh
token is a 14-day httpOnly cookie the BFF holds, and exchanging it requires
reaching the backend.

For a crew on a site with no signal, this means:

- Within ~15 minutes of going offline, no cached bearer token is valid.
- A page reload offline has no token at all, valid or otherwise.
- Therefore **any offline read must be served from cache without
  revalidating authorization**, because there is nothing to revalidate
  against.

This is not a bug to fix. Shortening the window is the whole point of a
15-minute token; lengthening it for offline would weaken every online
session to serve a case that may not exist.

---

## 4. What a cache means for tenant isolation

The most important sentence in `CLAUDE.md` is that **PostgreSQL RLS is the
enforcement boundary, not application code**. Every guarantee in this system
— tenant isolation, the `client` role's row scope, `field_crew`'s
assigned-only scope — is enforced at the moment a query runs, under
`app.current_tenant`.

A cached response has already left that boundary. Whatever is in the cache is
readable by whoever holds the device, with no policy evaluated and no token
required. Concretely, on a **shared site tablet**:

- Cached `/api/my-tasks` from crew member A is readable by crew member B
  after A logs out, unless the cache is scoped per user and cleared on
  logout.
- `POST /auth/logout` revokes the refresh token server-side; it does not and
  cannot reach a Cache Storage entry.
- Multi-company membership (migration 0031) makes this sharper: one person
  may hold memberships in several companies and switch between them. A cache
  keyed by URL alone would serve company A's payload to the same person
  acting as company B — the exact cross-tenant read RLS exists to prevent,
  reintroduced client-side.

**Any cache must therefore be keyed by `(user_id, active_company_id)` and
cleared on logout and on company switch.** That is a requirement, not a
refinement. It is also the part most likely to be got wrong quietly, since
nothing fails loudly when a cache key is too coarse.

---

## 5. What the existing architecture gets right for this

Three decisions already in place help, and are worth not undoing:

- **Everything goes through the BFF.** All browser calls are same-origin
  `/api/*` Route Handlers, so one service worker `fetch` handler intercepts
  the entire API surface uniformly. There is no cross-origin case to special
  case.
- **Optimistic concurrency is a body field, not a header.**
  `app/services/concurrency.py` uses `expected_updated_at` in the request
  body specifically because the BFF's `apiFetch` forwards a fixed header
  allowlist and would drop `If-Match`. A queued request stored as
  `(method, url, body)` therefore replays with its guard intact — a header
  form would have been silently stripped by the queue as well.
- **`PATCH /tasks/{id}` carries no stale-write guard at all**, so it is
  last-write-wins today. Queue-and-replay does not make it worse, but a
  status queued at 09:00 and flushed at 17:00 will overwrite whatever
  happened in between. If offline is built, that route should gain
  `expected_updated_at` **and** the queue should surface the resulting 409 to
  the user rather than discarding it.

---

## 6. Rough shape, if it is built

Not a plan — a sketch to price the decision.

| Piece | Notes |
|---|---|
| Manifest + icons | Trivial. `frontend/public/` has no manifest today; this is greenfield. |
| Service worker registration | Must not be registered on `(platform)` routes — the operator console is a different trust tier with its own cookie, and must never share a cache with the product UI. |
| API response cache | Keyed by `(user_id, active_company_id, url)`. Cleared on logout and company switch. Read-through for the five `field_crew` read routes only. |
| Write queue | Two request shapes. IndexedDB, replayed on reconnect, with a per-item idempotency key so a retried daily log does not double-post. |
| Conflict surfacing | A flush that returns 409 or 403 must be shown, not swallowed. A silently dropped daily log is worse than no offline support. |
| CSP resolution | §2 — the real cost, and the one that needs a decision before anything else is worth building. |
| E2E | Playwright can drive offline via CDP. The existing `security-headers.spec.ts` assertions must keep passing. |

The queue and the cache are each a few days. §2 is the whole risk.

---

## 7. The question this was written to sharpen

PRD §8 question 1 asks whether field crew usage *requires* offline support.
It cannot be answered from the code, and this document does not pretend to
answer it. It can be made much more specific:

> When a crew member is on site with no usable signal, are they trying to
> (a) **flip a task status**, (b) **file a daily log**, or (c) **look
> something up** — and how often does the third one matter?

That split matters because the three have very different prices:

- **(a) and (b) alone** are the write queue: no cached HTML, no CSP change,
  no offline page load. It only helps a session that started online and lost
  signal — which, for someone who opened the app in the truck and walked
  into a basement, may be the entire real-world case.
- **(c) requires a cached app shell**, and therefore requires resolving §2 —
  a change to the app's central security control — plus §4's per-user cache
  scoping.

**A useful intermediate exists.** The write queue can be built without any
CSP work and without caching a single document, and it covers both of the
only two writes this role has. If the answer to the question above is mostly
(a) and (b), the feature is small and safe. If it is (c), it is a security
architecture change and should be scoped as one.

The cheapest way to find out is to ask three foremen what they were doing
the last time the app failed them on site — not to build either version.

---

## Sources

Everything above is derived from the code as of `d635ae2`:
`app/routers/tasks.py`, `app/routers/projects.py`, `app/services/project_lookup.py`,
`app/services/concurrency.py`, `app/config.py`, `frontend/middleware.ts`,
`frontend/app/layout.tsx`, `frontend/contexts/AuthContext.tsx`,
`frontend/app/(app)/api/auth/login/route.ts`, and `frontend/CLAUDE.md`'s CSP
section. PRD §5.2 and §8 for scope and the open question.
