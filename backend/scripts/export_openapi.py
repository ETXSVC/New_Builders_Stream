"""Exports the app's OpenAPI schema as deterministic JSON on stdout.

Two consumers depend on the committed snapshot this produces
(backend/openapi.json):

1. The backend CI schema-diff gate: regenerates the schema and diffs it
   against the committed snapshot, so any route/schema change that isn't
   accompanied by a regenerated snapshot (and regenerated frontend types)
   fails loudly instead of drifting silently.
2. `frontend`'s `npm run generate:api-types`, which reads the snapshot
   file directly (openapi-typescript accepts a local path) — no live
   backend required, and `lib/api/types.ts` stays deterministic against
   the committed schema.

Importing `app.main` requires the five no-default `Settings` fields to be
present in the environment (see app/config.py), but building the schema
never touches the database/Redis — dummy values suffice:

    DATABASE_URL=x MIGRATIONS_DATABASE_URL=x TEST_DATABASE_URL=x \
    JWT_SECRET=x INTEGRATION_TOKEN_ENCRYPTION_KEY=x \
    python scripts/export_openapi.py > openapi.json

(Locally, the repo-root .env satisfies them without any of that.)

sort_keys=True so the output is byte-stable across runs regardless of dict
construction order — a requirement for the CI `diff` to be meaningful.
"""
import json

from app.main import app

if __name__ == "__main__":
    print(json.dumps(app.openapi(), indent=2, sort_keys=True))
