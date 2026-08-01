# Changelog

Notable changes to Builders Stream. Newest first.

This starts at the point a changelog was added; it is not backfilled to
the first commit. Reconstructing months of history from git log after the
fact produces something that looks authoritative and is quietly wrong, so
earlier work is left to `git log` and the design docs, which were written
alongside it.

Entries record **what changed for someone using or operating the system**.
Refactors appear only when they change behaviour or an interface.

## Unreleased

### Fixed

- Background jobs are enqueued **after** the request transaction commits.
  Previously a rolled-back request still queued an accounting-sync message
  naming a row that would never exist, and the worker burned all three
  retries against it.
- Dead-lettered Dramatiq messages are logged. Retry exhaustion was
  previously silent for every actor.
- Catalog `unit_rate` is quantized on write. `POST` echoed `5.678` while
  `Numeric(12,2)` stored `5.68`, so the next `GET` disagreed with the
  response that created the row.
- A client logging in lands on `/projects`; the e2e suite had asserted
  `/dashboard`, which a client is redirected away from.
- The estimate builder re-seeds when the estimate id changes, so
  "Duplicate as new draft" no longer leaves the previous estimate's lines
  on screen.
- Superseded list loads no longer overwrite fresher state, including on
  the projects list, where the loader appends and so duplicated rows.
- The API commits before responding, so a caller acting immediately on a
  returned id no longer races the commit and gets a 404.
- The platform console answered `200` before its write had committed, so
  an operator's tier change could read back as its old value moments
  later. Every transaction-holding dependency is now asked for on the
  exit stack that closes before the response is sent, and a test enforces
  it rather than a comment.
- `/forgot-password` and `/reset-password` were unreachable while signed
  out — the app shell replaced them with the login page, which is exactly
  the state their users are in.

### Added

- **A team directory** (`/team`). Each company keeps its own record of the
  people in it — names, address, phone numbers, trade and photo — held
  against the membership rather than the person, so somebody working for
  two builders has two records and neither company can read the other's.
  The trade list is company-managed. Reads are admin and project manager;
  writes are admin.
- **Your own details, on `/account`.** A member edits their own name,
  address, phones and photo without an admin doing it for them. The
  company's private notes about somebody, and the trade it files them
  under, stay with the admin — and the notes are not shown to their
  subject at all.
- **Assignee pickers say who people are.** Task assignment offered the
  name on somebody's login; it now offers the company's own name for them
  and their trade ("Rosa Okafor · Electrician").
- **Password reset by email.** There was previously no way back into a
  forgotten account short of editing the database. The link works once,
  expires in an hour, and asking again invalidates the last one. An
  account with two-factor authentication still needs its code — otherwise
  a reset by email would quietly reduce it to one factor. Completing a
  reset signs out every session that account holds.
- **Mail goes out under each company's own name.** Set on the branding
  tab; blank means the company name. Invitations, signature requests,
  expiry notices and password resets all use it.
- **A company can send through its own mail server** (Catalog → Email
  server), for domains with SPF and DKIM published. Credentials are
  encrypted at rest and never returned by the API, a host on a private or
  reserved network is refused, and a Test button sends a real message and
  reports what the server said. Turning it off falls back to Builders
  Stream's own relay without discarding the settings.
- `backend/scripts/send_test_email.py` — proves an operator's SMTP
  configuration by sending one message, instead of inviting somebody and
  hoping.


- Optimistic concurrency on six PATCH routes via `expected_updated_at`:
  a stale write is refused with 409 instead of silently overwriting
  someone else's edit.
- Bill of Materials: vendors, materials, receipts, and BOM generation from
  an approved estimate.
- A generated database ERD (`docs/13-database-erd.md`).
- `CONTRIBUTING.md` and an ADR index (`docs/adr/`).

### Changed

- **Every page is now rendered per request.** The Content-Security-Policy
  carries a per-request nonce, so `script-src` no longer depends on
  `'unsafe-inline'` — an injected script does not run. Prerendered HTML
  cannot carry a per-request value, so static prerendering is gone: each
  page render reaches the Node process rather than being served as a
  prebuilt file. Worth knowing when sizing the frontend container.
- Production boot refuses to start when `SMTP_HOST` is set while
  `SMTP_FROM_ADDRESS` is still the `no-reply@localhost` default — mail
  from an unroutable sender is rejected or bounces into nothing, and the
  invitation is lost with nothing in the logs.
- The design docs were re-derived against the live schema (44 tables, 41
  under row-level security), and the repo-root `CLAUDE.md` shed its
  frontend half to `frontend/CLAUDE.md`.
- Production boot refuses to start when `INTEGRATION_TOKEN_ENCRYPTION_KEY`
  equals `JWT_SECRET`, or when SMTP credentials are configured with
  STARTTLS disabled.
- Destructive confirmations are inline and consistent; `window.confirm` is
  gone. Tab surfaces implement the full ARIA tabs pattern, including
  keyboard navigation and reachable panels.
- `Caddyfile.api` gained an upload-size cap and HSTS; both Caddyfiles are
  now validated in CI.
- Dependabot no longer reopens the TypeScript 7 and ESLint 10 majors,
  which are blocked upstream (`openapi-typescript` pins `typescript@^5.x`;
  `eslint-plugin-react` has no eslint-10-compatible release).
