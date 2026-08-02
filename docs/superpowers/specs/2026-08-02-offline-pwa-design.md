# Offline support — scoping

**Status: scoping only. No implementation proposed.**

PRD §8 question 1 asks *"Does field crew usage require offline-capable
mobile/PWA support?"* — and the framing of that question is what made the
first version of this document scope the wrong thing.

**The actual driver is an estimator on site**, capturing estimate
information where there is no service so it is ready when they get signal.
That is a different role (`admin`/`project_manager`, not `field_crew`), a
much larger write surface, and — unlike the field crew case — it genuinely
requires the expensive branch.

Both cases are documented below, because the difference between them is the
whole decision: one is a few days' work with no security implications, and
the other is a change to the app's central security control.

---

## 1. The estimator case — what actually happens on site

An estimator standing in a building enters line items against the company's
cost catalog, and wants a total. That is three server round-trips, in order:

| Step | Route | Depends on |
|---|---|---|
| 1 | `POST /estimates` | — |
| 2 | `PUT /estimates/{id}/lines` (batch replace) | the id from step 1 |
| 3 | `POST /estimates/{id}/calculate` | steps 1 and 2 |

Plus **reads that are not optional**: the cost catalog (they are picking
items from it) and the markup profiles (they select one).

Four things make this materially harder than the field-crew case.

### 1.1 The catalog must be readable offline, so the app shell must load offline

An estimator does not open the app in the office and keep the tab alive
through the drive. They arrive on site, open the app, and there is no signal.
That is a **cold start while offline**, which means the HTML document itself
must come from cache — which is exactly what §3 says the CSP forbids.

The field-crew case could be served by "cache data, never documents,"
because a crew member plausibly opens the app before losing signal. The
estimator case cannot.

### 1.2 `unit_rate_snapshot` is copied at *server write* time, not capture time

This is the correctness problem, and it is subtle.

`PUT /estimates/{id}/lines` copies `unit_rate_snapshot` from the resolved
catalog item's `unit_rate` **at the moment the route runs**, deliberately —
the schema's historical-immutability rule exists so a later catalog edit
cannot retroactively change what an estimate shows.

Queue-and-replay breaks that rule from the other direction. An estimator
captures a line on Tuesday at $50/unit. Someone raises the catalog rate to
$60 on Wednesday. The queue flushes on Thursday, and the estimate silently
records **$60** — a rate the estimator never saw and did not quote. Nothing
errors; the number is simply wrong, and it is wrong in the direction of a
number a customer was given verbally on site.

The rule was written assuming capture and write are the same instant.
Offline makes them days apart, and the existing design has no way to express
"the rate as of when I captured this."

**Fixing it is an API change**, not a client concern: `PUT /lines` would
need to accept the captured rate (and reject or flag a mismatch), or accept
a `captured_at` the server resolves the rate against. Either is a real
design decision with an audit dimension — an estimator should probably not
be able to assert an arbitrary rate.

### 1.3 The write chain has ordering dependencies

The field-crew writes are two independent, self-contained requests. These
three are a chain: step 2 needs the server-assigned estimate id from step 1.

So the queue is not a flat list of replayable requests. It needs either
client-generated ids the server accepts, or a dependency graph where a
queued item is rewritten with the id its predecessor returned. The latter is
the usual answer and is fiddly to get right — particularly on partial
failure, where step 1 succeeded and step 2 got a 409.

### 1.4 Totals are a server step, and the rounding rule is exact

`POST /estimates/{id}/calculate` is deliberately a separate explicit step.
An estimator offline therefore sees **no total** unless the client
replicates the pipeline: line totals, category subtotals, overhead, profit,
tax (a documented no-op at `Decimal("0")` for Phase 2), and the rounding
rule — *only the final total is quantized*, `ROUND_HALF_UP` via
`app/core/money.py`'s `CENTS`.

A client-side reimplementation that rounds at a different step produces a
total that disagrees with the server's by cents, on the document a customer
signs. If offline totals are needed, that arithmetic has to be shared or
specified precisely enough to be reproduced exactly — and then tested
against the server's, or it will drift.

### 1.5 Also: estimates are tier-gated

Every mutating estimate route carries `require_module("estimation")`. A
queued write can come back **403** at flush time if the tenant's tier or
per-tenant override changed while they were offline. The queue must surface
that, not retry it — it will never succeed.

---

## 2. The field-crew case, for contrast — and it is cheap

Derived from `require_role(...)` on every route. A `field_crew` user can
perform exactly **two writes** in the entire product:

| Route | Shape | Offline character |
|---|---|---|
| `PATCH /tasks/{id}` | `status` **only**, own task only | Small enum, no contention |
| `POST /projects/{id}/daily-logs` | Create | **Append-only** |

Daily logs are immutable by construction — no update or delete route, and
migration 0004 does `REVOKE UPDATE, DELETE ON daily_logs, documents FROM
app_user` at the database level. A replayed create cannot conflict; it can
only *duplicate*, which is an idempotency key, not a merge strategy. Task
status is a small enum on a row only that user may touch, so two crew members
cannot race.

Both are independent requests with no ordering, no id dependency, no
server-side recomputation, and no capture-time semantics. **This case is a
write queue and nothing else** — no cached documents, no CSP change, a few
days' work.

It is worth keeping visible precisely because it is not what was asked for:
if estimator capture is the goal, the field-crew feature is not a stepping
stone toward it, and building it first buys very little of the harder thing.

---

## 3. The blocker: a cached app shell cannot satisfy the CSP

`app/layout.tsx` sets `export const dynamic = "force-dynamic"`, and
`middleware.ts` builds the CSP per request around a fresh 16-byte nonce that
Next stamps into the inline bootstrap scripts. `frontend/CLAUDE.md` records
why prerendering was given up for it:

> Prerendered HTML cannot carry a per-request nonce; it would ship a
> build-time value, the browser would compare it against the header's fresh
> one, and every page would fail to hydrate under its own policy —
> **silently**, because nothing in the app itself errors.

A service worker serving a cached HTML document is prerendering by another
name. `e2e/security-headers.spec.ts` asserts every script tag carries the
response's own nonce and that a real load logs no CSP violation, so relaxing
the policy is not available either.

**Three ways out:**

1. **A dedicated offline entry point with its own static CSP** — hashed
   script allowlist, no nonce. Smallest blast radius, but the offline
   experience becomes a separate mini-app rather than the product UI. For
   estimate capture specifically this may be the right answer: it is one
   focused screen, not the whole product.
2. **Hash-based CSP for the bootstrap** — build-time hashes instead of
   per-request nonces. Restores prerendering and makes caching viable
   everywhere, but changes the app's central security control, and
   `'strict-dynamic'` plus a nonce is what currently makes `script-src`
   meaningful.
3. **Cache data, never documents.** Preserves the CSP exactly — and does not
   solve the estimator case at all, per §1.1.

Option 1 is the one worth costing first.

---

## 4. Credentials and the cache boundary

Unchanged by the re-scoping, and both still apply.

**The access token is 15 minutes and lives in React state only.** The
refresh token is a 14-day httpOnly cookie the BFF holds, and exchanging it
needs the network. An estimator offline for an afternoon has no valid
credential, and a cold page load offline has no token at all — so **any
offline read is served without revalidating authorization**, because there
is nothing to revalidate against. That is not a bug to fix: the short window
is the point, and lengthening it would weaken every online session.

**A cache sits outside RLS entirely.** `CLAUDE.md`'s central claim is that
PostgreSQL RLS is the enforcement boundary, not application code — every
tenant guarantee is evaluated at query time under `app.current_tenant`. A
cached payload has already left that boundary; whatever is in it is readable
by whoever holds the device, with no policy evaluated.

For the estimator case this is sharper than for a crew tablet, because the
thing being cached is **the company's entire cost catalog** — its pricing,
which is commercially sensitive and is the one dataset a competitor would
want. Multi-company membership (migration 0031) makes it sharper again: one
person may hold memberships in several companies, so a cache keyed by URL
alone would serve company A's catalog while the user is acting as company B.

**Any cache must be keyed by `(user_id, active_company_id)` and cleared on
logout and on company switch.** That is a requirement, not a refinement, and
it is the part most likely to be got wrong quietly — nothing fails loudly
when a cache key is too coarse.

---

## 5. What the existing architecture gets right

- **Everything goes through the BFF.** All browser calls are same-origin
  `/api/*` Route Handlers, so one service worker `fetch` handler covers the
  entire API surface with no cross-origin special cases.
- **Optimistic concurrency is a body field, not a header.**
  `app/services/concurrency.py` uses `expected_updated_at` in the body
  because the BFF forwards a fixed header allowlist and would drop
  `If-Match`. A queued `(method, url, body)` therefore replays with its
  guard intact — a header form would have been stripped by the queue too.
- **Catalog reads already walk to exhaustion.** `CatalogPanel` and
  `CatalogItemsTab` use `useCursorAll`, so "the whole catalog, client-side"
  is already the access pattern; caching it is not a new shape. Note the
  known limit recorded in `lib/use-cursor-list.ts`: that walk is unbounded,
  and a ten-thousand-item catalog is four hundred requests to prime a cache.

---

## 6. What to decide, in order

1. **§1.2 first — the rate-snapshot semantics.** It is the only item here
   that can produce a *wrong number on a signed document*, and it is an API
   design question that does not depend on any of the client work. It is
   worth resolving even if offline is never built, because it is latent in
   any "draft now, submit later" flow.
2. **§3 — which CSP route.** This is the cost driver. Option 1 (a dedicated
   offline capture screen) is plausibly much cheaper than it sounds, because
   estimate capture is one screen rather than the product.
3. **Then the queue**, which is the least interesting part: dependency
   ordering, idempotency keys, and surfacing 403/409 at flush rather than
   swallowing them.

The field-crew write queue (§2) is a genuinely separate, much smaller
feature. It should be decided on its own merits rather than bundled — it
shares almost nothing with the estimator case beyond the words "offline
support."

---

## Sources

Derived from the code as of `d635ae2`: `app/routers/estimates.py`,
`app/routers/tasks.py`, `app/routers/projects.py`,
`app/services/estimate_calculation.py`, `app/services/concurrency.py`,
`app/services/project_lookup.py`, `app/core/tier_gating.py`,
`app/config.py`, migration 0004, `frontend/middleware.ts`,
`frontend/app/layout.tsx`, `frontend/contexts/AuthContext.tsx`,
`frontend/lib/use-cursor-list.ts`, and `frontend/CLAUDE.md`'s CSP section.
PRD §5.2 and §8 for scope and the open question.
