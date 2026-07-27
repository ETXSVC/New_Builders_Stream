# Builders Stream — Frontend Architecture

**Date:** 2026-07-27
**Related:** [Technical Architecture](03-technical-architecture.md) · [API Specification](05-api-specification.md)

The largest previously-undocumented surface in the repository. This
describes what is there and why, so the next person changing it does not
have to infer the rules from eight examples.

`frontend/` is the Next.js 16 App Router application. `marketing-site/`
and `marketing/` are a separate, pre-existing static site and its copy —
unrelated, and not covered here.

## 1. The BFF is the only thing that talks to the API

The browser never calls the FastAPI backend directly. Every product
request goes to a Next Route Handler under `app/api/**`, which forwards it
server-side.

This is load-bearing in three ways that are easy to undo by accident:

- **`NEXT_PUBLIC_API_URL` is read at run time, on the server.** It is
  never inlined into the client bundle. `frontend-ci.yml` boots the
  standalone image beside a stand-in backend specifically to prove this,
  because a build-time inline would work in every environment except a
  rebuilt-once, deployed-many one.
- **The forwarded header set is a fixed allowlist** — `Content-Type`,
  `Authorization`, `X-Tenant-ID`, `X-Forwarded-For`. Anything else is
  dropped. This is why optimistic concurrency travels as an
  `expected_updated_at` body field rather than `If-Match`: a custom header
  would not survive the hop.
- **The real client IP survives only because the BFF forwards it.**
  Caddy sets a spoof-safe `X-Forwarded-For`; the BFF passes it on; uvicorn
  is started with `--proxy-headers`. Break any link and the register rate
  limiter keys on the frontend server's own address, and the ESIGN
  evidence records the wrong IP — legally significant, and silent.

## 2. Route groups

- `app/(app)/` — authenticated product UI. Everything behind login.
- `app/(marketing)/` — public pages.
- `app/api/` — the BFF handlers.

`middleware.ts` gates `(app)` on the *presence* of the refresh cookie. It
deliberately does not validate it — middleware has no backend access — so
a page can still render for a stale cookie. Components that ask a user for
credentials must therefore gate on a *confirmed* session, not merely on
"not hydrating"; `components/account/MfaPanel.tsx` documents the concrete
trap.

## 3. Types are generated, never written

`lib/api/types.ts` is generated from the committed `backend/openapi.json`
snapshot by `npm run generate:api-types`. Neither file is hand-edited.
After a backend route or schema change, regenerate the snapshot first,
then the types; CI's schema-diff gate fails if the snapshot drifts from
the code.

Route *docstrings* land in the snapshot's `description` fields, so editing
a Python docstring moves both generated files. That is expected, not a
mistake.

## 4. Data loading

`lib/use-cursor-list.ts` owns list loading: the backend's opaque cursor
pagination, replace-or-append, the error surface, and the generation guard
that drops superseded responses.

The guard deserves its own paragraph, because it has been got wrong twice.
It must sit **above** the `!response.ok` branch; below it, only the error
path is protected and the success write — the one that actually corrupts
the list — stays exposed. And a superseded response must also skip its
`setLoading(false)`, or it clears the spinner belonging to the request
still in flight.

`lib/use-latest-only.ts` is the same idea for single-record loaders.

`app/(app)/integrations/page.tsx` deliberately does not use the hook: its
envelope is `{connected_at, records, next_cursor}` rather than `{items,
next_cursor}`, and a 404 there is a normal pre-connection state rather
than an error. Fitting it would mean an envelope mapper and a per-status
escape hatch used exactly once.

## 5. Components

`components/ui/` holds the primitives — hand-written in the shadcn/ui
style over Radix, not installed as a dependency. Two carry rules worth
knowing:

- **`confirm-button.tsx`** is the *only* destructive-confirm pattern.
  `window.confirm` is banned: it cannot be styled or carry a busy state,
  and Playwright auto-dismisses it unless a spec registers a dialog
  handler — so a test that "covered" delete would silently exercise
  *cancel* and pass.
- **`tabs.tsx`** is the only tablist. It implements the full ARIA pattern:
  roving tabindex, arrow keys with wrap, focus following selection, and
  the `aria-controls`/`aria-labelledby` pair. Hand-rolled tablists got the
  labelling right and the interaction wrong three times.

Rows that render a repeated control (`Delete`, `Void`) need an
`aria-label` naming the target. Without it the control is ambiguous both
to a screen reader and to a test.

## 6. State

React state and context only — no Redux, no TanStack Query, no SWR. Auth
lives in `contexts/AuthContext`; everything else is local to the component
or supplied by a hook.

This is a deliberate floor, not an oversight. Adding a data-fetching
library is a reasonable future decision; it should be taken on its own
merits rather than arriving as a side effect of deduplicating some
loaders.

One React rule this codebase has been bitten by: **a `useState`
initializer runs only on mount.** Navigating between two ids on the same
route does not remount, so a component seeded from props needs
`key={entity.id}` to reset. `app/(app)/estimates/[id]/page.tsx` is the
worked example.

## 7. Tests

Playwright, in `frontend/e2e/`, against the real stack — no mocked
backend, except where a spec deliberately forces a failure with
`page.route`.

`failOnFlakyTests` is on in CI: a test that passes on retry **fails the
job**. Do not weaken this to go green. It has caught two real product bugs
that a passing check had been hiding, and one wrong assertion of its own.
