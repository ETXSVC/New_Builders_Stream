"""Render docs/feature-status.html to docs/Builders-Stream-Feature-Status.pdf.

    backend/.venv/Scripts/python.exe backend/scripts/render_feature_status.py

One command, because the alternative is a four-line invocation pasted from a
comment, and a document nobody can regenerate stops being regenerated. Paths
are resolved from this file's own location, so the working directory does not
matter.

xhtml2pdf rather than a second toolchain: it is already a dependency for the
estimate PDF export, and this is the same Jinja2 -> HTML -> PDF shape. The
tradeoff is its reduced CSS support (no flex, no grid) - see the source file's
own header comment.

**This renders; it does not derive.** The statuses live in the HTML and are
re-derived by hand from the code, the migrations and the test suite. Nothing
here checks them, and nothing could: "shipped" versus "awaiting credentials"
is a judgment about what a feature needs next, not a property of the source
tree.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SOURCE = REPO_ROOT / "docs" / "feature-status.html"
OUTPUT = REPO_ROOT / "docs" / "Builders-Stream-Feature-Status.pdf"


def main() -> None:
    from xhtml2pdf import pisa

    if not SOURCE.is_file():
        raise SystemExit(f"{SOURCE} does not exist")

    with SOURCE.open(encoding="utf-8") as source, OUTPUT.open("wb") as output:
        result = pisa.CreatePDF(source.read(), dest=output, encoding="utf-8")

    if result.err:
        raise SystemExit(f"xhtml2pdf reported {result.err} error(s); {OUTPUT} may be incomplete")

    # ASCII only in anything printed: stdout here is a Windows console more
    # often than not, and a cp1252 encoder mangles anything else mid-sentence.
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
