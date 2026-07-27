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

### Added

- Optimistic concurrency on six PATCH routes via `expected_updated_at`:
  a stale write is refused with 409 instead of silently overwriting
  someone else's edit.
- Bill of Materials: vendors, materials, receipts, and BOM generation from
  an approved estimate.
- A generated database ERD (`docs/13-database-erd.md`).
- `CONTRIBUTING.md` and an ADR index (`docs/adr/`).

### Changed

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
