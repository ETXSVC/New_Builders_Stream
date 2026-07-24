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
RUN pip install --no-cache-dir --prefix=/opt/python .

FROM base AS dev
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN pip install --no-cache-dir -e ".[dev]"
COPY . .
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
