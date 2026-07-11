"""Task 2.14: minimal placeholder — necessary extension beyond this task's
own literal "Files" list, not a deviation from intent (same category as
Phase 1's `PATCH /projects/{id}` and this phase's PDF-tracking columns on
`estimates`: the schema doc/plan didn't spell it out, but skipping it would
leave the task's own stated verification step impossible to satisfy).

The `worker` service in `docker-compose.yml` (design decision #2) runs
`dramatiq app.tasks.estimate_pdf` — Dramatiq's CLI imports that module
before starting its event loop, so it must exist and import cleanly for
`docker compose up -d worker` to bring up a healthy, non-crash-looping
container at all. Task 2.14's own text is explicit that "no actor is
defined yet in this task (that's Task 2.15)", so this file intentionally
defines zero `@dramatiq.actor`-decorated functions — only importing
`app.tasks.broker` (registering the Redis broker as Dramatiq's global
default, a required side effect before Dramatiq's CLI can do anything
useful with this module, per `broker.py`'s own docstring).

Task 2.15 EXTENDS this exact file with the real PDF-export actor — it does
not create a new one.
"""

from app.tasks import broker  # noqa: F401 - import-time side effect (see module docstring)
