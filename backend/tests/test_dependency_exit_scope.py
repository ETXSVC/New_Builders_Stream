"""A dependency that commits must be asked for with `scope="function"`.

FastAPI runs a dependency's exit code on one of two stacks
(`fastapi/routing.py`):

    async with AsyncExitStack() as request_stack:      # closes LAST
        async with AsyncExitStack() as function_stack: # closes FIRST
            response = await f(request)
        await response(scope, receive, send)           # response sent here

A generator dependency defaults to the **request** stack, which closes
*after* the response has been sent. Every dependency in this app that holds
a transaction open across the route handler commits in exactly that exit
code — so on the default scope the client is handed a 200 for a write that
has not committed yet, and a caller quick enough to re-read beats it.

`get_current_user`'s docstring records the first time this bit: 400
create-then-accept cycles produced 5 x 404 on a row that had just been
created. `scope="function"` was the fix, and the rule was then written down
in a docstring and enforced by nothing. The console's `get_platform_admin`
(migration 0023) duly missed it a few months later, and the symptom was
worse to read: the e2e spec saved a tier change, got its 200, re-read the
list and rendered the OLD tier — intermittently, because the window is one
commit wide, so it looked like a flaky test rather than a lost write.

So this file is the gate the rule never had. Two halves, because either one
alone can be satisfied by an accident:

  * every call site passes the scope, and
  * the set of dependencies this applies to is DISCOVERED from the code
    rather than transcribed here — a third one added tomorrow fails this
    file until it is either fixed or explicitly excused.
"""
import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

# Where a FastAPI dependency that commits could plausibly live. Narrow on
# purpose: `app/db.py`'s `session_scope` is also a committing async
# generator, but it is an `@asynccontextmanager` used directly by routers,
# never a `Depends()` target, and the exit-stack rule does not apply to it.
DEPENDENCY_MODULES = (APP_ROOT / "core" / "deps.py", APP_ROOT / "core" / "platform_deps.py")

# The floor that stops this whole file from passing vacuously if the AST
# walk ever stops matching (a rename of `Depends`, a move to `Annotated`
# defaults, an import alias). Every sweep in this suite carries one.
MINIMUM_CALL_SITES = 8


def _committing_dependencies() -> set[str]:
    """Async generators that commit after their `yield` — found, not listed.

    Undecorated on purpose: an `@asynccontextmanager` is not a dependency,
    and FastAPI's exit-stack scope has nothing to say about it.
    """
    found = set()
    for path in DEPENDENCY_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.decorator_list:
                continue
            body = list(ast.walk(node))
            yields = any(isinstance(n, ast.Yield) for n in body)
            commits = any(
                isinstance(n, ast.Attribute) and n.attr == "commit" for n in body
            )
            if yields and commits:
                found.add(node.name)
    return found


def _depends_call_sites(names: set[str]):
    """(file, line, dependency, keywords) for every `Depends(<name>, ...)`."""
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            called = node.func
            called_name = (
                called.attr if isinstance(called, ast.Attribute) else getattr(called, "id", None)
            )
            if called_name != "Depends":
                continue
            first = node.args[0]
            dependency = (
                first.attr if isinstance(first, ast.Attribute) else getattr(first, "id", None)
            )
            if dependency in names:
                yield path.relative_to(APP_ROOT), node.lineno, dependency, node.keywords


def test_the_committing_dependencies_are_the_ones_we_think():
    """Non-vacuity for the discovery half. If this shrinks to an empty set,
    every other assertion below passes without checking anything."""
    assert _committing_dependencies() == {"get_current_user", "get_platform_admin"}, (
        "the set of transaction-holding dependencies changed. A new one must "
        "either take `scope=\"function\"` at every call site (see this "
        "module's docstring) or be excused here with the reason it cannot "
        "commit late."
    )


def test_every_committing_dependency_is_requested_with_function_scope():
    names = _committing_dependencies()
    offenders = []
    for path, lineno, dependency, keywords in _depends_call_sites(names):
        scope = next(
            (
                kw.value.value
                for kw in keywords
                if kw.arg == "scope" and isinstance(kw.value, ast.Constant)
            ),
            None,
        )
        if scope != "function":
            offenders.append(f"{path}:{lineno} Depends({dependency}, scope={scope!r})")

    assert offenders == [], (
        "these dependencies commit in their exit code but are asked for on "
        "FastAPI's request stack, which closes AFTER the response is sent — "
        "so the client can read back the pre-write state: " + repr(offenders)
    )


def test_the_call_site_sweep_is_not_vacuous():
    """Non-vacuity for the call-site half, which is the one an AST change
    would silently empty."""
    sites = list(_depends_call_sites(_committing_dependencies()))
    assert len(sites) >= MINIMUM_CALL_SITES, (
        f"only {len(sites)} Depends() call sites found — the walk has stopped "
        "matching how this codebase declares dependencies, and the assertion "
        "above is no longer checking anything."
    )
    # Both dependencies, not just whichever one has more routes: the console
    # is exactly the surface that missed this rule once.
    assert {dependency for _, _, dependency, _ in sites} == _committing_dependencies()
