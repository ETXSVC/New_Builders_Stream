"""A router may not import another router.

CLAUDE.md has always said it: "Modules should only reach another module's
data through its service layer (`app/services/`), never by querying
another module's tables directly — a convention enforced by review, not
tooling." The last clause was the problem. Eight cross-router imports had
accumulated, all reaching for helpers whose leading underscore said
*private* and whose import site said otherwise:

    app/routers/bills.py                     -> projects, subcontractors
    app/routers/expenses.py                  -> projects
    app/routers/invoices.py                  -> projects
    app/routers/tasks.py                     -> projects
    app/routers/change_orders.py             -> projects
    app/routers/subcontractor_assignments.py -> projects, subcontractors

Review had signed off on every one of them individually, which is what
review does with a small, obviously-correct diff; the cost only shows up
in aggregate. `projects.py` had become a library six modules linked
against, so its "private" helpers could not be changed without auditing
six other routers, and importing a router for one function drags in that
router's whole import graph.

The concrete failure this prevents is not stylistic. The shared helpers
carry authorization: `get_project_or_404` applies field_crew's
assigned-only scope and the `client` role's row scope (migration 0019).
A module that cannot import them conveniently writes its own — which is
exactly what `app/routers/bom_lines.py` did, a bare `select(Project)`
with neither scope. Nothing caught it, because nothing was looking.

This test is deliberately structural rather than behavioural. It cannot
tell you the scoping is right; `test_client_role_isolation.py` and
`test_tenant_isolation_phase3.py` do that. What it guarantees is that
there is exactly ONE place per entity where that scoping can be got
right, so those tests keep covering every caller.

`app/main.py` is the one legitimate importer of routers — wiring them
into the app is its entire job — and it is not a router, so it is
outside the sweep by construction.
"""
import ast
import pathlib

ROUTERS = pathlib.Path(__file__).resolve().parent.parent / "app" / "routers"


def _cross_router_imports() -> list[str]:
    """Every `app.routers.X` import that appears inside `app/routers/`.

    An AST walk rather than a text search: these modules discuss each
    other's helpers at length in docstrings and comments (correctly — the
    ordering rationale in `subcontractor_assignments.py` is worth
    reading), and prose about a module is not a dependency on it.

    Covers both `import app.routers.x` and `from app.routers.x import y`,
    including the relative form (`from .projects import ...`), which is
    the obvious way to reintroduce the coupling once the absolute form is
    blocked.
    """
    offenders: list[str] = []
    for path in sorted(ROUTERS.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # `level > 0` is a relative import; inside app/routers/ a
                # level-1 import resolves to a sibling router.
                if node.level > 0 or (node.module or "").startswith("app.routers"):
                    target = node.module or ""
                    offenders.append(f"{path.name} -> {'.' * node.level}{target}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app.routers"):
                        offenders.append(f"{path.name} -> {alias.name}")
    return offenders


def test_no_router_imports_another_router():
    offenders = _cross_router_imports()
    assert offenders == [], (
        "Routers must reach shared logic through app/services/, not through "
        "each other (CLAUDE.md, 'Layering within a module'). Move the helper "
        "into a service module and import it from there:\n  "
        + "\n  ".join(offenders)
    )


def test_the_sweep_actually_reads_the_routers():
    """Guards against a vacuous pass.

    Every assertion above is satisfied by an empty file list — a renamed
    directory, a changed glob, or a packaging change that stops shipping
    `app/routers/` would turn this file into a no-op that still reports
    green. Pinning a floor on what was scanned makes that fail loudly
    instead.
    """
    scanned = [p.name for p in ROUTERS.glob("*.py") if p.name != "__init__.py"]
    assert len(scanned) >= 20, f"only {len(scanned)} routers scanned: {sorted(scanned)}"
    # The two modules whose helpers the six offenders above reached for.
    # If either stops being a router, this test's premise needs revisiting.
    assert "projects.py" in scanned
    assert "subcontractors.py" in scanned


def test_the_shared_lookups_live_in_services():
    """The other half of the rule: the helpers are actually somewhere a
    router can legitimately import them from.

    Without this, the boundary test above is satisfiable by deleting the
    callers or by re-inlining a private copy into each router — the
    duplication that let `bom_lines.py` drift in the first place.
    """
    from app.services.project_lookup import get_project_or_404, with_field_crew_scope
    from app.services.subcontractor_lookup import get_subcontractor_or_404

    for fn in (get_project_or_404, with_field_crew_scope, get_subcontractor_or_404):
        assert fn.__module__.startswith("app.services."), fn.__module__


def test_every_project_scoped_read_goes_through_the_one_chokepoint():
    """No router may hand-roll `select(Project).where(Project.id == ...)`.

    This is the check that would have caught `bom_lines.py`. The import
    sweep above cannot: a router that writes its own unscoped lookup
    imports nothing at all, so it passes the boundary rule while
    reintroducing precisely the bug the boundary rule exists to prevent.

    Matching is on the AST — a `select(Project)` call whose chained
    `.where(...)` compares `Project.id` — so the many legitimate
    `select(Project)` queries that scope by something else (a join from
    `phases`, a `company_id` filter, the list route's own paginated query)
    are untouched. `projects.py` itself is exempt: it is the module the
    entity belongs to, and its list route legitimately builds Project
    queries.

    Only Project is swept. The other by-id chokepoints CLAUDE.md names
    (`_get_estimate_or_404`, `_get_change_order_or_404`,
    `_get_invoice_or_404`) each live in the router that owns the entity
    and have no cross-module callers, so "some other module wrote its own
    version" is not a failure mode they have yet. Project is the entity
    six other modules need. Add a sweep here if that changes.
    """
    offenders = []
    for path in sorted(ROUTERS.glob("*.py")):
        if path.name in {"__init__.py", "projects.py"}:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _is_call_to(node, "where")):
                continue
            if not _receiver_selects(node, "Project"):
                continue
            for arg in node.args:
                if _compares_attribute(arg, "Project", "id"):
                    offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], (
        "These routers look up a Project by id directly instead of calling "
        "app.services.project_lookup.get_project_or_404, which is where "
        "field_crew's assigned-only scope and the client role's row scope "
        "(migration 0019) are applied:\n  " + "\n  ".join(offenders)
    )


def _is_call_to(node: ast.Call, name: str) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr == name


def _receiver_selects(node: ast.Call, model: str) -> bool:
    """True if the `.where(...)` chain hangs off `select(<model>)`."""
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    while isinstance(receiver, ast.Call):
        func = receiver.func
        if isinstance(func, ast.Name) and func.id == "select":
            return any(isinstance(a, ast.Name) and a.id == model for a in receiver.args)
        receiver = func.value if isinstance(func, ast.Attribute) else None
    return False


def _compares_attribute(node: ast.AST, model: str, attr: str) -> bool:
    """True if `node` is (or contains) a comparison against `<model>.<attr>`."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Compare):
            continue
        for side in (child.left, *child.comparators):
            if (
                isinstance(side, ast.Attribute)
                and side.attr == attr
                and isinstance(side.value, ast.Name)
                and side.value.id == model
            ):
                return True
    return False
