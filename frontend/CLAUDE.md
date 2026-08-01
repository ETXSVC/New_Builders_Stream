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

Security headers live in `next.config.ts` and apply in dev too, with one
deliberate exception: **`script-src` gains `'unsafe-eval'` when
`NODE_ENV === "development"` and must never carry it in production.**
React's development build and Turbopack's HMR runtime both call `eval()`
— without the exception, `next dev` throws "eval() is not supported in
this environment" on every page, which is exactly what an unconditional
CSP produced. React never uses `eval()` in production, so the relaxation
buys nothing there and costs the policy its point.
`e2e/security-headers.spec.ts` asserts every header and, under CI
specifically, that `'unsafe-eval'` is **absent** — `e2e-ci` builds and
serves a production frontend, so that assertion runs against the real
artifact. HSTS is deliberately NOT here: it belongs at Caddy
(`deploy/Caddyfile`), being meaningless without TLS and harmful over dev
HTTP. A nonce-based strict CSP that would remove `'unsafe-inline'`
entirely is a tracked follow-up, not a blocker.

`lib/use-cursor-list.ts` is the shared loader for cursor-paginated lists,
and the one to reach for in new code. It carries the stale-response guard
— a generation ref checked **above** `if (!response.ok)`, so a superseded
request can write neither data nor an error — which one of eight
copy-pasted loaders had silently been missing.

**Six surfaces use it today** (leads, estimates, subcontractors, and the
three billing panels); roughly seventeen others still hand-roll the fetch,
so do not assume a list page you are editing is on the hook — check.
It is split into `useCursorListCore` (the loader) and `useCursorList` (the
core plus AuthContext's bearer token) because the platform console cannot
call the wrapper: `useAuth()` throws without a provider, and the console has
none. Reach for the core, not a copy, if you ever need it outside `(app)`.
`integrations/page.tsx` is a deliberate permanent exception: different
response envelope, and a 404 there means "not connected", not an error.
