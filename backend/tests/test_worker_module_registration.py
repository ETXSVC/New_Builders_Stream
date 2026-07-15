"""Task 4.16 follow-up: every app/tasks module that defines a Dramatiq actor
must be listed on docker-compose.yml's worker `command:` line, or its
messages land in the dead-letter queue silently ("Received message for
undefined actor") — nothing errors loudly. Task 4.16's live E2E run caught
exactly that: app.tasks.seat_usage (Task 3.23),
app.tasks.flag_overdue_financial_records (Task 3.45), and
app.tasks.accounting_sync (Task 4.12) had each been added to app/tasks/
without being added to the worker command — three separate tasks made the
identical omission, months apart, because the only guardrail was a YAML
comment. This test converts that comment into an enforced invariant."""
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
TASKS_DIR = BACKEND_DIR / "app" / "tasks"
COMPOSE_FILE = BACKEND_DIR.parent / "docker-compose.yml"


def test_every_actor_module_is_listed_on_the_worker_command_line():
    actor_modules = set()
    for path in TASKS_DIR.glob("*.py"):
        if path.name == "__init__.py":
            continue
        # Match actual actor-registering CODE — dramatiq.actor called with an
        # open paren, as a decorator (@dramatiq.actor(...)) or as this
        # codebase's established undecorated-fn wrap
        # (name = dramatiq.actor(...)(_name)) — not prose: broker.py's
        # docstring MENTIONS `@dramatiq.actor` backtick-quoted with no call
        # paren while defining no actor, which a bare substring check
        # wrongly flagged (caught by this test's own first run).
        if re.search(r"dramatiq\.actor\(", path.read_text(encoding="utf-8")):
            actor_modules.add(f"app.tasks.{path.stem}")

    assert actor_modules, (
        "sanity check: no actor-defining modules found under app/tasks/ — "
        "if genuinely true, this test (and the worker service) are obsolete; "
        "more likely the detection heuristic broke"
    )

    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(r"^\s*command:\s*dramatiq\s+(.+)$", compose_text, re.MULTILINE)
    assert match, (
        "docker-compose.yml no longer has a `command: dramatiq ...` line — "
        "if the worker service's invocation changed shape, update this "
        "test's parsing to match, don't delete the invariant"
    )
    listed_modules = set(match.group(1).split())

    missing = sorted(actor_modules - listed_modules)
    assert missing == [], (
        f"app/tasks modules defining Dramatiq actors but NOT listed on "
        f"docker-compose.yml's worker command line (their messages would be "
        f"silently dead-lettered in the running stack): {missing}"
    )
