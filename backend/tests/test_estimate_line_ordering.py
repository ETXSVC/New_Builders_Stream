"""Every query that returns estimate line items orders them deliberately.

Migration 0036 gave `estimate_line_items` a `position` column because all
three read paths ordered by `id` — a random `uuid4` — so an estimator's
arrangement was discarded on save, including on the PDF a customer signs.

Fixing those three is not the durable part. The durable part is that a
*fourth* read path added next month gets the same treatment without anyone
remembering this, which is why this is an AST sweep rather than three more
assertions. Same shape as `test_module_boundaries.py` and
`test_monitoring_config.py`: assert against what the code actually contains,
not against a transcribed list, and carry a non-vacuity floor because a sweep
over an empty set passes trivially.

The rule: **a `select()` that returns whole `EstimateLineItem` rows must order
by `position`.** Selecting a single column (`select(EstimateLineItem.id)` for
an existence check) is not covered — there are no rows to put in an order.
Anything that genuinely does not need ordering goes on `_UNORDERED_BY_DESIGN`
with a reason, and there is no third option that passes.
"""

import ast
import pathlib

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"

# A query over whole line-item rows whose result order is genuinely not
# user-visible. Each entry needs a reason, because "it seemed fine" is how the
# original bug survived three separate read paths.
_UNORDERED_BY_DESIGN: dict[str, str] = {
    "services/estimate_calculation.py": (
        "Sums line totals into category subtotals. Nothing here is displayed in "
        "row order, and the breakdown it returns is sorted by category name with "
        "its own comment explaining why."
    ),
}


def _module_paths() -> list[pathlib.Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _selects_whole_line_item_rows(call: ast.Call) -> bool:
    """True when `select(...)` is passed the mapped class itself.

    `select(EstimateLineItem, CostCatalogItem.category)` returns rows and is
    covered. `select(EstimateLineItem.id)` returns one column and is not —
    a scalar has no order to get wrong.
    """
    if not (isinstance(call.func, ast.Name) and call.func.id == "select"):
        return False
    return any(
        isinstance(arg, ast.Name) and arg.id == "EstimateLineItem" for arg in call.args
    )


def _enclosing_statement(tree: ast.Module, target: ast.Call) -> ast.stmt | None:
    """The statement the select sits in — where its `.order_by()` would be."""
    best: ast.stmt | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        for child in ast.walk(node):
            if child is target:
                # Innermost enclosing statement wins; walk yields outer first.
                best = node
                break
    return best


def _find_unordered_line_item_selects() -> tuple[list[str], int]:
    offenders: list[str] = []
    ordered_count = 0

    for path in _module_paths():
        source = path.read_text(encoding="utf-8")
        if "EstimateLineItem" not in source:
            continue
        tree = ast.parse(source)
        relative = path.relative_to(APP_ROOT).as_posix()

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _selects_whole_line_item_rows(node)):
                continue

            statement = _enclosing_statement(tree, node)
            segment = ast.get_source_segment(source, statement) if statement else None
            if segment is None:
                offenders.append(f"{relative}: could not read the enclosing statement")
                continue

            if "EstimateLineItem.position" in segment:
                ordered_count += 1
            elif relative not in _UNORDERED_BY_DESIGN:
                offenders.append(
                    f"{relative}:{node.lineno} selects whole EstimateLineItem rows without "
                    "ordering by position"
                )

    return offenders, ordered_count


def test_every_line_item_row_query_orders_by_position():
    offenders, _ = _find_unordered_line_item_selects()
    assert offenders == [], (
        "These queries return estimate line items in whatever order Postgres "
        "happens to produce, which for this table means arbitrary order — the bug "
        "migration 0036 exists to fix. Add `.order_by(EstimateLineItem.position.asc(), "
        "EstimateLineItem.id.asc())`, or add the module to _UNORDERED_BY_DESIGN with a "
        "reason:\n  " + "\n  ".join(offenders)
    )


def test_the_sweep_actually_found_the_queries_it_is_guarding():
    """The non-vacuity floor.

    Every assertion above passes if the sweep matches nothing at all — a
    renamed import or a refactor to a query builder would silently turn this
    file into a no-op that stays green forever. Three ordered read paths exist
    today: the estimate detail route, the recalculate route, and the PDF task.
    """
    _, ordered_count = _find_unordered_line_item_selects()
    assert ordered_count >= 3, (
        f"Expected at least 3 position-ordered line-item queries, found {ordered_count}. "
        "Either they were removed, or this sweep has stopped recognising them and is no "
        "longer guarding anything."
    )


def test_the_allowlist_names_modules_that_exist():
    """An allowlist entry for a deleted module silently excuses nothing, and
    reads as though a decision is still being honoured when it is not."""
    for relative in _UNORDERED_BY_DESIGN:
        assert (APP_ROOT / relative).is_file(), (
            f"_UNORDERED_BY_DESIGN names {relative}, which no longer exists"
        )
