# Multi-stage: `production` (the default, last stage) is what
# docker-compose.prod.yml runs; the `dev` target preserves the previous
# dev-image behavior for docker-compose.yml (which sets build.target: dev
# and bind-mounts the source anyway).

FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONUNBUFFERED=1

FROM base AS builder
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*
# A real (non-editable) install into an isolated prefix that the production
# stage copies wholesale — the app runs from site-packages, not a
# bind-mounted source tree.
COPY pyproject.toml ./
COPY app ./app
# `[observability]` is included so enabling Sentry is a pure environment
# change — set SENTRY_DSN and restart, no image rebuild during whatever
# incident made you want error reporting in the first place. The package
# is inert without a DSN (app/core/observability.py returns before
# importing it), so the only cost is image size.
#
# `[stripe]` is here for exactly the same reason and with the same
# inertness: app/services/billing.py builds FakeStripeClient unless
# STRIPE_API_KEY is set, and RealStripeClient imports the SDK inside
# __post_init__ rather than at module scope, so an image carrying the
# package but no key never touches it. Leaving it out would mean the switch
# to real billing needed a rebuild AND a redeploy, and — worse — that the
# `httpx` the SDK's async path requires (a `dev`-only dependency otherwise)
# is absent from the one artifact that actually ships.
RUN pip install --no-cache-dir --prefix=/opt/python ".[observability,stripe]"

FROM base AS dev
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
# Dependencies first, from pyproject alone, so editing source does not
# reinstall the world on every rebuild.
RUN pip install --no-cache-dir -e ".[dev]"
COPY . .
# ...then redo the editable install, now that `app/` actually exists.
#
# The install above ran with only pyproject.toml present, so setuptools had
# no package to find and wrote an editable finder whose MAPPING is EMPTY —
# the install is real (pip lists it, `Editable project location: /app`) but
# imports nothing. It went unnoticed because `uvicorn app.main:app` and
# `python -m ...` both put the working directory on sys.path and so never
# needed it; `python scripts/whatever.py` does NOT (sys.path[0] is
# scripts/), and died with `ModuleNotFoundError: No module named 'app'` —
# including on the invocation scripts/grant_platform_admin.py documents in
# its own docstring.
#
# --no-deps keeps this to a metadata rewrite against the layer above rather
# than a second dependency resolution. The `production` stage is unaffected:
# its builder copies `app/` BEFORE installing, and installs non-editably.
RUN pip install --no-cache-dir --no-deps -e .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM base AS production
COPY --from=builder /opt/python /usr/local
# migrations/ + alembic.ini ship in the image so the prod compose's one-shot
# `migrate` service can run `alembic upgrade head` from the same artifact
# that serves traffic; scripts/ for export_openapi and future operational
# one-offs.
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts
# Non-root runtime. /data/documents pre-created and owned by appuser so the
# named documents volume initializes with writable ownership on first
# mount.
RUN useradd --uid 1000 --create-home appuser \
    && mkdir -p /data/documents \
    && chown -R appuser:appuser /data/documents /app
USER appuser
EXPOSE 8000
# --proxy-headers + --forwarded-allow-ips="*": trusts X-Forwarded-For from
# any peer. Safe ONLY because the prod compose gives this container no
# published port — every possible peer (the Next.js BFF, on the internal
# network) is a trusted hop by construction. Never publish this port
# without revisiting that flag. Worker count via WEB_CONCURRENCY (uvicorn
# honors it); default 1 suits a small VM.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
