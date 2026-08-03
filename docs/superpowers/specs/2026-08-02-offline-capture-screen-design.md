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

## 1. The CSP is probably not a blocker — and I got this wrong

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

**This needs a spike before anything is built on it.** The reasoning is
sound but Next's App Router streams RSC payloads and does client-side
navigation, and I have not proven that a cached document + cached
`/_next/static` chunks hydrate offline in this app specifically. That spike
is half a day and it determines whether this feature is small or large.

If it holds: **no CSP change, no separate entry point, no static-CSP
screen.** The offline capture screen is an ordinary `(app)` route.

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

1. **Run the §1 spike.** Everything else is priced off the answer. Half a
   day, and it turns this from "a security-architecture change" into "a
   screen."
2. **Nonce reuse.** If the spike holds, caching a document means replaying
   its nonce for the cache's lifetime. Bounded by cache TTL. Decide the
   bound, and write down that it was decided.
3. **What a 409 at flush does.** The guard makes the conflict visible; it
   does not say what happens next. Options: re-open the draft with both
   rates shown and let the estimator re-confirm (probably right), or
   auto-accept the new rate (fast, and exactly the silent re-pricing the
   guard exists to prevent). This is the main UX question and it is not a
   small one — the estimator may have already given the customer a number.
4. **Whether the catalog may be cached at all**, given it is the company's
   pricing. A product/security call, not an engineering one.

## 7. What this deliberately does not include

Offline **viewing** of existing estimates, projects, or documents; offline
PDF; offline anything for the field crew (a separate, much smaller feature
— parent spec §2); and any change to the estimate builder itself.

---

## Sources

Code as of `8d9603c`: `frontend/middleware.ts`, `frontend/app/layout.tsx`,
`frontend/contexts/AuthContext.tsx`, `frontend/lib/use-cursor-list.ts`,
`backend/app/schemas/estimate.py`, `backend/app/schemas/estimate_line_item.py`,
`backend/app/routers/estimates.py`, `backend/app/config.py`, and
`frontend/e2e/security-headers.spec.ts`.
