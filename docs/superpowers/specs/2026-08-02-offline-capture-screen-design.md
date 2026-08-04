# Offline estimate capture — screen design

Follows `2026-08-02-offline-pwa-design.md`, which established that the
driver is an estimator on site capturing estimate information for later,
not a field crew.

**This document corrects that one on its central claim.** §3 there said a
cached app shell "cannot satisfy the CSP." Having read `middleware.ts`
properly, that is overstated, and the correction makes this feature
substantially cheaper than the spec priced it. §1 below explains why, and
what still needs proving.

---

## 1. The CSP is not a blocker — and I got this wrong first time

The claim was that a cached HTML document carries a stale nonce while the
CSP header is minted fresh per request, so the page fails to hydrate.

That is exactly right for **static prerendering**, which is why
`app/layout.tsx` is `force-dynamic`: the HTML is built once at build time
and the header is built per request, so the two can never agree.

It is **not** right for a service worker. The nonce and the policy are
minted together in one pass of `middleware()` — the nonce goes into the
request headers (Next stamps it onto the inline bootstrap scripts) and the
same string goes into the response's `Content-Security-Policy`. A service
worker caching the **whole response, headers included**, replays a document
whose script tags and whose policy carry *the same* nonce. The browser
evaluates the cached HTML against the cached header, they agree, and the
page hydrates.

`e2e/security-headers.spec.ts` asserts every script tag carries "the
response's own nonce" — which a cached response satisfies, because the
nonce it carries is its own.

**What this costs, and it is not nothing:** a cached document replays one
nonce for the life of the cache entry, so the nonce stops being
per-request. A nonce's job is to stop an *injected* inline script from
executing; an attacker who can inject into the document can already read
the nonce out of it, so the practical loss is smaller than "we removed the
nonce," but it is a real weakening and should be a stated decision rather
than a side effect. Bounding the cache entry's lifetime bounds the reuse.

### Spike result, 2026-08-02: confirmed

Run against a **production** build (`next start`, so no `'unsafe-eval'`) with
a naive service worker caching `response.clone()` for navigations and
`/_next/static`:

| Case | Result |
|---|---|
| Worker registered, `/login` cached, offline reload | **Hydrates.** Renders, and typing into a controlled React input works — so the JS ran, not just the markup. **Zero CSP violations** on the console. |
| **Control:** no worker, offline reload | **Fails outright** (`page.reload()` rejects) |

The control is the half that makes it evidence: without it, the first case
proves only that a page rendered, not that the worker is why.

**So: no CSP change, no separate entry point, no static-CSP screen.** The
offline capture screen can be an ordinary `(app)` route.

The one thing that must be preserved is *how* the response is cached:
`cache.put(request, response.clone())`. Re-fabricating a `Response` from the
body would drop the `Content-Security-Policy` header, the cached document's
nonce would have no matching policy, and the page would fail silently —
which is the failure this section originally predicted, reachable by getting
the caching wrong rather than by caching at all.

<details>
<summary>Reproduction (the spike code itself was not kept)</summary>

```js
// public/sw.js — naive on purpose; no versioning, no per-user key
const CACHE = "csp-spike-v1";
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode !== "navigate" && !url.pathname.startsWith("/_next/static")) return;
  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      (await caches.open(CACHE)).put(request, response.clone()); // clone: keeps headers
      return response;
    } catch {
      const cached = await caches.match(request);
      if (cached) return cached;
      throw new Error("offline and not cached");
    }
  })());
});
```

Register it from a throwaway client route, then in Playwright: wait for
`navigator.serviceWorker.controller`, visit `/login` online, `setOffline(true)`,
`reload()`, assert the sign-in button is visible and a controlled input
accepts typing, with a `page.on("console")` filter for
`/content security policy/i` asserting none. Add the no-worker control.

Must run against `next start`, not `next dev` — dev adds `'unsafe-eval'` to
`script-src` and would mask the failure being looked for.

</details>

The spike's worker was deliberately **not** merged: it has scope `/`, no
versioning, and no per-user cache key, so a route that installs it would
cache every document unkeyed by user — the exact RLS-boundary problem §4
warns about.

---

## 2. What the screen is

One route — call it `/estimates/capture` — that does the smallest useful
thing:

1. Pick a **lead or project** (from cache).
2. Pick a **markup profile** (from cache).
3. Add **catalog items** with quantities (from cache).
4. Save as a **local draft**.
5. **Flush** when connectivity returns.

It is deliberately not the estimate builder. The builder does calculation,
PDF export, send-for-signature and e-signature — none of which can work
offline and none of which an estimator needs standing in a building.

### It cannot create the lead or project

`POST /estimates` requires `project_id` **or** `lead_id`, and the router
verifies the referenced row exists and is in an eligible state (a lead must
be `estimating`/`qualified`/`won`). Neither can be conjured client-side.

So offline capture works for **"I am visiting a lead that is already in the
system"** and not for **"I met someone new on site."** That is a real
product limitation and probably an acceptable one — an estimator visiting a
site usually has an appointment, and the appointment came from a lead. It
should be stated rather than discovered.

---

## 3. The write chain, and why it is now safe

Three calls, in order, with a dependency:

| Step | Route | Notes |
|---|---|---|
| 1 | `POST /estimates` | Returns the id steps 2 and 3 need |
| 2 | `PUT /estimates/{id}/lines` | Carries `expected_unit_rate` per line |
| 3 | `POST /estimates/{id}/calculate` | Totals |

**Step 2 is why this is buildable now.** Before PR #128,
`unit_rate_snapshot` was copied from the catalog at replace-time, so a
draft captured Tuesday and flushed Thursday would silently record
Thursday's rate — a number the estimator never saw and may have quoted.
`expected_unit_rate` now travels with the write, and the server refuses the
save with a 409 naming both rates if the catalog moved.

That turns the worst failure mode of offline capture from **silent
mispricing** into **a visible conflict at flush**. It does not resolve the
conflict — see §6 — but a conflict a human is shown is a different class of
problem from a wrong number nobody sees.

### Queue shape

Not a flat replayable list: step 2 needs the id step 1 returns. The draft
should be stored as **one logical unit** (lead/project ref, markup profile,
lines with captured rates) and the three calls made in sequence at flush
time, rather than as three independent queued requests. That avoids a
dependency graph entirely, and it matches how the user thinks about it —
one estimate, saved or not saved.

Partial failure is then a single question: step 1 succeeded and step 2
409'd, leaving an empty estimate server-side. The flush must either
complete it or delete it, and "leave an empty estimate lying around" is not
an acceptable third option.

---

## 4. What must be cached

| Data | Why | Notes |
|---|---|---|
| Cost catalog | Items are picked from it | Already walked to exhaustion by `useCursorAll` — the access pattern exists |
| Markup profiles | Selected per estimate | Small |
| Leads/projects the estimator may attach to | Step 1 needs a real id | Could be limited to recent/assigned |
| The document + `/_next/static` chunks | §1 | Only if the spike confirms |

**Keyed by `(user_id, active_company_id)`, cleared on logout and on company
switch.** This is the requirement from the parent spec §4 and it is not
optional: RLS stops at the network boundary, and multi-company membership
means one person legitimately holds two tenants' data. A URL-keyed cache
would serve company A's catalog while the user acts as company B.

The catalog is also the commercially sensitive part — it is the company's
pricing. A cache of it sitting on a phone is a different exposure from a
cache of task statuses, and that should be a conscious decision.

---

## 5. Auth, which is the part with no clean answer

The access token is **15 minutes** and lives in React state only. The
refresh token is a 14-day httpOnly cookie the BFF holds, and exchanging it
needs the network.

- **Offline reads** must be served from cache with no token, because there
  is no valid one and no way to get one. The service worker cannot check
  authorization; the cache key is the only thing standing between users.
- **Flush** happens when connectivity returns, and at that point the BFF
  can refresh normally — the refresh cookie is still valid for 14 days, so
  an estimator who was offline all day flushes fine.
- **A draft older than the refresh token** cannot be flushed by that
  session at all. Fourteen days is generous, but the failure should be
  explicit: the draft survives, and the estimator is asked to sign in.

---

## 6. What to decide before building

**All four are now decided — see §8.** The list is kept as written so the
decisions can be read against the questions they answer.

1. ~~Run the §1 spike.~~ **Done 2026-08-02, confirmed** — see §1. This is
   a screen, not a security-architecture change.
2. **Nonce reuse.** Caching a document replays its nonce for the life of
   the cache entry, so the nonce stops being per-request. Bounded by the
   cache TTL. Decide the bound, and write down that it was decided rather
   than letting it fall out of whatever the worker happens to do.
3. ~~What a 409 at flush does.~~ **Largely answered by PR #131**, which
   built the recovery for the *online* case: the 409 reports every
   conflicting line at once and carries the current rate for each, the
   builder shows old → new per item, and a "Use new rates" button updates
   the draft **without saving** — the estimator sees the new total and
   presses save themselves. Offline flush should reuse that component
   rather than invent a second policy.

   One difference the online case does not have, and it is the part still
   open: **at flush there may be nobody looking at the screen.** Adopting
   rates requires a human, so a background flush that hits a 409 has to
   park the draft and surface it, not resolve it. Decide what "park and
   surface" means — a badge, a list of drafts needing attention, something
   else — before the flush is written, because a queue that silently
   retries a 409 forever is the same silent failure in a new place.
4. **Whether the catalog may be cached at all**, given it is the company's
   pricing. A product/security call, not an engineering one — **and the one
   that should be answered first.** The entire design assumes an estimator
   can pick from a cached catalog offline; if the answer is no, this
   feature does not exist in its current shape and nothing below is worth
   building.

   **Answered 2026-08-03: yes, on a device that asks for it** — §8.1.

## 7. What this deliberately does not include

Offline **viewing** of existing estimates, projects, or documents; offline
PDF; offline anything for the field crew (a separate, much smaller feature
— parent spec §2); and any change to the estimate builder itself.

---

## 8. Decisions taken, 2026-08-03 — before writing any of it

### 8.1 The catalog may be cached, but only on a device that asks

**Decided: yes, with priming as an explicit act.** §6.4 asked whether the
company's pricing may sit on a phone at all, and the answer that makes it
acceptable is not a policy sentence — it is that nothing is cached until
somebody presses **"Make available offline"** on the capture screen. A
device that never presses it holds no catalog, which means the exposure
follows the estimator who needs it rather than every browser that ever
loaded the app.

The same screen shows what it is holding, when it was primed, and a
**"Remove offline data"** button that clears it. An exposure the person
carrying it can see and revoke is a different thing from one they cannot.

Keyed by `(user_id, active_company_id)` and cleared on logout and on
company switch, per §4 — not optional, and the part most likely to be got
wrong quietly.

### 8.2 A cached document is served for 24 hours, then it is not

**Decided: 24 hours, enforced by the worker, on serve rather than on
write.** §6.2's question is the nonce: a cached document replays its own
nonce for as long as the cache entry lives, so the cache lifetime *is* the
nonce lifetime.

Twenty-four hours is chosen because it is the shape of the actual job — an
estimator drives out in the morning and is back in signal by evening — and
because bounding it at the *serve* is what makes the bound real: a stamp
written at cache time and checked before the response is handed over,
rather than an eviction policy that only runs when something else happens
to trigger it.

**What it costs, stated rather than discovered:** an estimator offline for
longer than a day cannot cold-start the app, and that includes reaching
drafts already captured. The drafts themselves are not lost — they are in
IndexedDB, which this bound does not touch — but they are unreachable
until the device sees the network again. That is the price of not letting
one nonce live indefinitely, and it is worth it because the failure is
visible and recoverable while an unbounded nonce is neither.

`/_next/static/*` is exempt: those URLs are content-addressed per build and
carry no nonce, so a TTL on them would buy nothing and cost the offline
start its JavaScript.

### 8.3 A 409 at flush parks the draft and waits for a human

**Decided.** §6.3 left the part the online case does not have: at flush
there may be nobody looking. So the flush **never adopts a rate on its
own** and **never retries a 409**. It moves the draft to
`needs_attention`, stores the conflicting lines with both rates, and stops.

The capture screen surfaces those drafts above everything else, and
resolving one uses the *same component* as the online builder — old → new
per line, a "Use new rates" button that updates the draft without saving,
and the estimator pressing save themselves. `RateConflictNotice` is
extracted for exactly this reason: two copies of that policy would drift,
and the drift would be silent re-pricing appearing in one of them.

A 403 (the tenant's tier or override moved while they were offline) parks
the same way with a different message, and is likewise never retried — it
will never succeed.

### 8.4 Partial failure keeps the estimate id rather than the empty estimate

§3 said the flush must either complete or delete an estimate whose step 1
succeeded and whose step 2 failed, and that leaving it lying around is not
an acceptable third option.

**Decided: complete it.** The draft keeps `estimate_id` from the moment
step 1 returns, persisted before step 2 is attempted, so resuming resumes
rather than creating a second estimate. Discarding a parked draft that
holds an id issues `DELETE /estimates/{id}` first and refuses to discard
locally if that call fails — which is the only path by which an empty
estimate could otherwise be orphaned.

### 8.5 Logout clears the cache; it does not delete unsent work

Cached reference data — catalog, markup profiles, leads and projects — is
cleared on logout and on company switch, along with the cached documents.

**Drafts are not.** They are the estimator's own unsent work, and deleting
them on logout would lose it silently at the exact moment somebody is least
expecting a destructive side effect. They stay keyed by
`(user_id, active_company_id)`, so a different user signing in on the same
device does not see them, and the capture screen shows only the active
identity's. The residual exposure is real and smaller than the catalog's: a
draft holds the rates for the lines it captured, not the whole price list.

---

## 9. As built, 2026-08-03 — and the two things the design did not predict

Shipped as designed: `/estimates/capture` (an ordinary `(app)` route, per
§1), `public/sw.js`, `lib/offline/*`, the draft store keyed by
`(user_id, active_company_id)`, and `RateConflictNotice` extracted so the
builder and the flush share one recovery policy. `e2e/offline-capture.spec.ts`
covers the cold start, the capture, the flush, and the 409-at-flush park,
with a control proving the worker's allowlist is real.

Two things were wrong in the design, both found by the e2e test rather than
by review, and both worth recording because they are invisible until the
app is actually run with the network switched off.

### 9.1 A cached document is not a cached page

§1 proved the *document* replays under its own CSP, and stopped there. The
first offline cold start rendered **nothing**: the HTML came from the cache
and every `/_next/static/*` chunk it referenced went to the network and
failed. Nothing errored — the document served perfectly — so the symptom
was a blank screen that reads as a broken app rather than as an incomplete
cache.

The fix is to prime the assets the cached document *names*, extracted from
its own script and link tags. That list is exactly right by construction:
it is what the browser will ask for when it replays that document. Priming
from what the current page happens to have loaded is a different list — an
estimator who arrived by client-side navigation loaded a different set of
chunks than a fresh document load will ask for.

The spike missed this because it cached `/_next/static` opportunistically
through the `fetch` handler and reloaded the page, which pulled the chunks
in on the way past. A prime-and-then-go-offline flow never makes those
requests through the worker at all.

### 9.2 `navigator.onLine` reads `true` with no network

The first version of the "do not redirect an offline cold start to /login"
guard asked `navigator.onLine`. Under Playwright's offline emulation that
flag stays **true**, so the guard never fired and the cached page hydrated,
redirected itself to `/login`, and failed to load it — a blank screen
again, by a completely different route. The same is true in the field
behind a captive portal or on a Wi-Fi with no route out, so this was a real
bug that the test happened to expose rather than a test artifact.

That forced a better answer than the design had: `AuthContext` now
distinguishes a refresh that was **refused** (the server answered no — end
the session) from one that never **arrived** (`sessionUnreachable` — keep
the session, retry every 10s). A 5xx counts as unreachable too, since the
BFF answers 502 when the backend is down and folding that into "signed out"
turns a backend restart into a mass logout.

Three consequences worth knowing:

- **A network blip no longer signs anyone out mid-session.** Previously any
  failed scheduled refresh called `clearSession()`. That was deliberate,
  but it was written for "the cookie expired", and it also fired for "the
  lift has no signal."
- **The capture screen's connection badge is evidence**, not a flag: it
  reads from whether the last send actually arrived.
- **Waiting drafts retry on a timer** (10s) as well as on the `online`
  event, because that event is a hint that may never come.

---

## Sources

Code as of `8d9603c`: `frontend/middleware.ts`, `frontend/app/layout.tsx`,
`frontend/contexts/AuthContext.tsx`, `frontend/lib/use-cursor-list.ts`,
`backend/app/schemas/estimate.py`, `backend/app/schemas/estimate_line_item.py`,
`backend/app/routers/estimates.py`, `backend/app/config.py`, and
`frontend/e2e/security-headers.spec.ts`.
