"""A join through `cost_catalog_item_id` must not drop free-form lines by accident.

Migration 0035 made `estimate_line_items.cost_catalog_item_id` nullable so an
estimator could add a line the catalog does not price. Three separate queries
joined through that column at the time, and **all three carried a comment
asserting the column could never be NULL** — written when that was true, and
silently false the moment the migration landed. An inner join through a
now-nullable column is not a join any more, it is a filter.

Two of them were wrong in ways that lost money:

* `estimate_calculation.py` — the estimate would total **less than the sum of
  its own lines**, silently.
* `estimate_pdf.py` — the line would vanish from the document a customer
  signs, while the total beneath it still included the money.

The third, `bom_generation_handler.py`, was found separately and later (by the
ordering sweep in #140, not by anyone reading it), and turned out to be
correct for a reason its comment did not give.

Three call sites, three separate discoveries, all after the fact. So this is a
gate rather than a fourth careful reading: **a join through
`EstimateLineItem.cost_catalog_item_id` is an OUTER join, or it is on
`_INNER_BY_DESIGN` with a reason.** There is no third option that passes.

Same shape as `test_estimate_line_ordering.py` and the other architectural
sweeps here: assert against what the code contains rather than a transcribed
list, and carry a non-vacuity floor, because a sweep that matches nothing
passes trivially and stays green forever.
"""

import ast
import pathlib

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"

_JOIN_TARGET = "EstimateLineItem.cost_catalog_item_id"

# Inner joins that mean to exclude free-form lines. Each needs a reason,
# because "it looked fine" is how three of these survived review.
_INNER_BY_DESIGN: dict[str, str] = {
    "services/bom_generation_handler.py": (
        "A bill of materials lists things to order, and a free-form line is work "
        "with no catalog item behind it — site cleanup, a permit fee. Excluding it "
        "is the intent here, unlike the calculation and PDF joins where excluding "
        "it lost money and lost work off a signed document."
    ),
}


def _module_paths() -> list[pathlib.Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _find_joins() -> tuple[list[str], int]:
    """Returns (inner joins needing justification, count of outer joins found)."""
    unjustified: list[str] = []
    outer_count = 0

    for path in _module_paths():
        source = path.read_text(encoding="utf-8")
        if _JOIN_TARGET not in source:
            continue
        tree = ast.parse(source)
        relative = path.relative_to(APP_ROOT).as_posix()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute) and node.func.attr in ("join", "outerjoin")):
                continue

            # The ON clause is an argument to this call, so the call's own
            # source segment is the right scope to look in — not the whole
            # statement, which may chain several joins.
            segment = ast.get_source_segment(source, node)
            if segment is None or _JOIN_TARGET not in segment:
                continue

            if node.func.attr == "outerjoin":
                outer_count += 1
            elif relative not in _INNER_BY_DESIGN:
                unjustified.append(f"{relative}:{node.lineno}")

    return unjustified, outer_count


def test_inner_joins_through_the_nullable_column_are_justified():
    unjustified, _ = _find_joins()
    assert unjustified == [], (
        "These INNER joins go through `cost_catalog_item_id`, which is nullable since "
        "migration 0035 — so they silently drop every free-form line, and whatever they "
        "compute is missing that work. Use `.outerjoin(...)`, or add the module to "
        "_INNER_BY_DESIGN with a reason:\n  " + "\n  ".join(unjustified)
    )


def test_the_sweep_actually_found_the_joins_it_is_guarding():
    """The non-vacuity floor.

    Two outer joins exist today — the calculation service and the PDF task —
    and both are load-bearing. If this count drops, either they were removed
    or this sweep has stopped recognising them and is guarding nothing.
    """
    _, outer_count = _find_joins()
    assert outer_count >= 2, (
        f"Expected at least 2 outer joins through {_JOIN_TARGET}, found {outer_count}."
    )


def test_the_allowlist_names_modules_that_exist():
    """An entry for a deleted module excuses nothing while reading as though a
    decision is still being honoured."""
    for relative in _INNER_BY_DESIGN:
        assert (APP_ROOT / relative).is_file(), (
            f"_INNER_BY_DESIGN names {relative}, which no longer exists"
        )
