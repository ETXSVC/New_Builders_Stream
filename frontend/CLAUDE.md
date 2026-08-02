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
