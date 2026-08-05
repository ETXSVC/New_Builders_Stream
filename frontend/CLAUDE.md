# Frontend (Next.js)

Loaded when working under `frontend/`. Moved out of the repo-root
`CLAUDE.md` so backend-only sessions do not pay for it — the content is
unchanged.

## Commands

Run from `frontend/`:

```bash
npm run dev             # next dev
npm run build
npm run lint            # eslint .
npm run test:e2e        # playwright test
# Regenerates lib/api/types.ts from the COMMITTED backend/openapi.json
# snapshot. Never hand-edit either file; after a backend route/schema
# change, regenerate the snapshot (backend/scripts/export_openapi.py)
# first, or CI's schema-diff gate fails.
npm run generate:api-types
```

## Architecture

Next.js App Router with route groups: `app/(app)/` (authenticated product
UI), `app/(marketing)/` (public pages), and `app/(platform)/` (the operator
console at `/platform`). The console is a **different trust tier and a
different session**: `POST /platform/auth/login` returns no refresh token, so
`lib/platform/session.ts` keeps the platform token itself in an httpOnly
`sameSite=strict` cookie whose lifetime comes from the response's
`expires_in_minutes` — it never reaches JavaScript, and one `middleware.ts`
gates both trees (selecting which cookie by path, with `/platform/login`
exempt). It has no `AuthProvider`/`AppShell` on purpose: the product nav would
offer links a platform token cannot open. `docs/14-frontend-architecture.md`
§2.1 has the rest. TypeScript API types in
`lib/api/types.ts` are generated from the committed `backend/openapi.json`
snapshot via `npm run generate:api-types` — never hand-edit either file;
after a backend route/schema change, regenerate the snapshot
(`backend/scripts/export_openapi.py`) and then the types (CI's schema-diff
gate fails if the snapshot drifts from the code). `marketing-site/` (static HTML/CSS/JS) and `marketing/` (copy docs)
are a separate, pre-existing marketing site, unrelated to the Next.js app.

Security headers live in two places, and the split is the point. The
constant ones (`X-Frame-Options`, `Referrer-Policy`, …) are in
`next.config.ts`. **The CSP is built per request in `middleware.ts`**,
because it carries a nonce — a fresh 16-byte value per response that Next
stamps into the inline bootstrap scripts it emits, which is what lets
`script-src` stop relying on `'unsafe-inline'`. Two consequences worth
knowing before touching either file:

- **Every page renders per request** (`export const dynamic` in
  `app/layout.tsx`). Prerendered HTML cannot carry a per-request nonce; it
  would ship a build-time value, the browser would compare it against the
  header's fresh one, and every page would fail to hydrate under its own
  policy — silently, because nothing in the app itself errors. Static
  prerendering is the accepted cost of the nonce.
- **`middleware.ts`'s matcher is no longer the auth boundary.** It now
  covers every non-asset path so the CSP reaches every document, and the
  auth scope lives in `PROTECTED_TREES` with one segment-safe `isUnder()`
  helper. A route not in that list is public — check it when adding one.

`'unsafe-inline'` remains in the `script-src` LIST deliberately: CSP3
browsers ignore it once a nonce and `'strict-dynamic'` are present, and
CSP2-only browsers fall back to it rather than breaking. `style-src` keeps
it for real — Next and Tailwind both inject style tags and there is no
style nonce pipeline. One exception is unchanged: **`script-src` gains
`'unsafe-eval'` when `NODE_ENV === "development"` and must never carry it
in production.**
React's development build and Turbopack's HMR runtime both call `eval()`
— without the exception, `next dev` throws "eval() is not supported in
this environment" on every page, which is exactly what an unconditional
CSP produced. React never uses `eval()` in production, so the relaxation
buys nothing there and costs the policy its point.
`e2e/security-headers.spec.ts` asserts every header, that the nonce is
per-request and that every script tag carries the response's own nonce
(a policy naming a nonce the HTML lacks is worse than the old one — it
looks stricter and blocks the app's own bootstrap), that a real page load
logs no CSP violation, and, under CI specifically, that `'unsafe-eval'` is
**absent** — `e2e-ci` builds and
serves a production frontend, so that assertion runs against the real
artifact. HSTS is deliberately NOT here: it belongs at Caddy
(`deploy/Caddyfile`), being meaningless without TLS and harmful over dev
HTTP.

## The service worker, and the two offline screens

`public/sw.js` exists for two screens, and only those two:
`/estimates/capture` — the **site survey**, which an estimator cold-starts on
site with no signal
(`docs/superpowers/specs/2026-08-02-offline-capture-screen-design.md`; note
the URL keeps the older "capture" wording deliberately, because it is in the
worker's allowlist and in documents already cached on real devices) — and
`/my-tasks`, which is the field crew's entire product and carries the only
two writes their role can make — a task's status and a daily log
(`docs/superpowers/specs/2026-08-04-field-crew-offline-queue-design.md`).
Adding a third means deciding what it caches and what its writes do when
they fail, not appending a string to `OFFLINE_PATHS`.

**A survey is not a quote, and the vocabulary is load-bearing.** A survey
records what was measured and happens online or offline; a quote is priced,
and only exists once the server has priced it. That is why the survey screen
shows no total (`LineRows`' `showSubtotal={false}`) — a running total there
would be a price produced by a screen that is not allowed to produce one.

`lib/offline/hooks.ts` holds what both screens share — whose data this is,
whether the server is reachable, and when to try again — precisely so the
two cannot drift on the answers. The flushes stay separate (`flush.ts` for
the estimator's three-call chain, `crew.ts` for the crew's two independent
writes); one function stretched over both would need a flag per difference.

Four things about the worker are easy to undo by accident.

- **It caches `response.clone()`, headers and all.** That is what makes a
  cached document legal under the CSP: the nonce and the policy are minted
  together in one `middleware()` pass, so a whole cached response carries a
  policy naming the nonce its own script tags carry. Re-fabricating a
  `Response` from the body drops `Content-Security-Policy`, and the page
  then fails **silently** — markup that never hydrates, with nothing
  erroring. The cost is nonce reuse for the cache entry's life, which is
  why a cached document is refused after **24 hours** (checked on serve,
  not by an eviction pass).
- **It caches the assets the cached document names**, extracted from that
  document's own script/link tags. Caching the HTML alone gives an offline
  cold start with no JavaScript — a blank page that looks like the app is
  broken rather than like the cache is incomplete. This was found by the
  e2e test, not by review.
- **It never caches `/api/*`.** A `Cache` is keyed by URL, and one person
  may hold memberships in two companies (migration 0031), so a URL-keyed
  cache of `/api/catalog/items` cannot express *whose* catalog it holds.
  Tenant data lives in IndexedDB (`lib/offline/store.ts`) keyed by
  `(user_id, active_company_id)`, cleared on logout and on company switch.
- **Nothing is registered or cached until the user presses "Make available
  offline."** The catalog is the company's pricing, and a cache sits outside
  RLS entirely.

**A queued write must be safe to send twice**, because the case the queue
exists for — the request arrives, the row commits, the response dies — is
indistinguishable from the request never arriving. Both of the crew's writes
carry a guard for it, and both are BODY fields rather than headers, because
the BFF forwards a fixed header allowlist and would drop a header silently
(the same trap `expected_updated_at` documents for `If-Match`):
`client_reference` on a daily log (a replay returns the original row —
migration 0034, and it matters because no runtime role can delete a daily
log, so a duplicate is permanent), and `expected_status` on a task PATCH
(409 if it moved, because `tasks` has no `updated_at` to compare against).

**`navigator.onLine` is not used to decide anything, and must not be.** It
reads `true` with no route to anything — behind a captive portal, on a dead
Wi-Fi, and under Playwright's own offline emulation, which is how this was
caught. AuthContext instead distinguishes a refresh that was **refused**
(session over — `clearSession()`) from one that never **arrived**
(`sessionUnreachable`, keep the session and retry every 10s). `AppShell`
reads that flag to keep a tokenless offline cold start on the capture
screen instead of redirecting it to `/login`, and the capture screen shows
"Offline" from the same evidence. The browser's `online`/`offline` events
are used only as hints that shorten a wait.

`lib/use-cursor-list.ts` is the shared loader for cursor-paginated lists,
and the one to reach for in new code. It carries the stale-response guard
— a generation ref checked **above** `if (!response.ok)`, so a superseded
request can write neither data nor an error — which one of eight
copy-pasted loaders had silently been missing.

**Every cursor-paginated surface now uses it** — 26 files import the module,
8 calling `useCursorList` (one page plus "Load more") and 15 calling
`useCursorAll` (walk to exhaustion, for a set the user chooses from rather
than reads through). `app/(app)/integrations/page.tsx` is the only remaining
hand-written loader, and deliberately so; see the exception below. A new list
surface should not be the second.

`lib/use-latest-only.ts` is a different tool and is still the right one:
seventeen files use it to guard a **single-object** fetch (a detail page, a
PDF panel), where there is no cursor to walk. Reach for it there and for the
cursor hooks here — a list that guards by hand is re-deriving what the hook
already owns.

It is split into `useCursorListCore` (the loader) and `useCursorList` (the
core plus AuthContext's bearer token) because the platform console cannot
call the wrapper: `useAuth()` throws without a provider, and the console has
none. Reach for the core, not a copy, if you ever need it outside `(app)`.
`integrations/page.tsx` is a deliberate permanent exception: different
response envelope, and a 404 there means "not connected", not an error.
