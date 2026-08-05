"""Run a takeoff provider over an eval corpus and report whether it clears the bar.

    python scripts/score_takeoff.py --corpus ./takeoff-corpus
    python scripts/score_takeoff.py --corpus ./takeoff-corpus --provider claude

`docs/superpowers/specs/2026-08-04-ai-blueprint-takeoff-scoping.md` §5.3
identifies the thing this feature is actually blocked on: an accuracy bar
stated as a number, which can only be answered against a corpus of real plan
sets with the takeoffs humans produced from them. This is the command that
answers it — and, under §5.2's provider-agnostic decision, the instrument
that compares one provider against another on the same evidence.

**It reports; it does not decide.** The threshold is a flag with a default,
printed alongside the result, because the bar is a number somebody chose and
should stay arguable rather than disappear into this script.

## The corpus format

A directory of `*.json` cases. One case is one plan set and the takeoff a
human produced from it:

    {
      "name": "riverside-duplex",
      "document": "sheets/riverside-duplex.pdf",
      "catalog": [
        {"id": "8f...", "category": "Framing", "name": "2x4 stud wall", "unit": "lf"}
      ],
      "expected": [
        {"cost_catalog_item_id": "8f...", "quantity": "142.00"}
      ]
    }

`document` is relative to the case file. `catalog` is the company's resolved
catalog **as it stood when the human did the takeoff** — not today's, or the
comparison measures catalog drift rather than the provider.

**Assembling this is the work, and it is the part no code can do.** A dozen
cases is enough to be informative; they have to be real, because a synthetic
plan set measures a provider's ability to read a synthetic plan set. Keep the
corpus OUT of this repository — it is customers' drawings — and point
`--corpus` at wherever it lives.

## Why the fake is the default here

With no `--provider`, this runs `FakeTakeoffProviderClient`, which proposes
whatever it was handed — nothing, for a corpus case. That looks useless and
is not: it exercises the whole path end to end, proves the corpus parses, and
gives a floor (0% acceptance) that any real provider must beat. Running it
against a corpus is the first thing to do after assembling one, before any
vendor credential exists.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from decimal import Decimal
from pathlib import Path

from app.services.takeoff_client import (
    CatalogEntry,
    TakeoffRequest,
    get_takeoff_client,
)
from app.services.takeoff_scoring import (
    CorpusScore,
    ExpectedLine,
    score_case,
)


def _load_case(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        case = json.load(handle)
    for required in ("name", "catalog", "expected"):
        if required not in case:
            raise SystemExit(f"{path}: case is missing required key {required!r}")
    return case


async def _score_corpus(corpus: Path, provider: str | None) -> CorpusScore:
    case_paths = sorted(corpus.glob("*.json"))
    if not case_paths:
        # ASCII only in anything this script emits: stdout here is a Windows
        # console more often than not, and a cp1252 encoder turns an em dash
        # into a replacement character mid-sentence.
        raise SystemExit(
            f"No *.json cases found in {corpus}. See this script's docstring for the "
            "corpus format, and note that assembling one is the actual prerequisite "
            "for this feature."
        )

    client = get_takeoff_client(provider)
    scores = []

    for path in case_paths:
        case = _load_case(path)
        catalog = [
            CatalogEntry(
                id=uuid.UUID(entry["id"]),
                category=entry["category"],
                name=entry["name"],
                unit=entry["unit"],
            )
            for entry in case["catalog"]
        ]

        pdf_bytes: bytes | None = None
        if case.get("document"):
            document_path = (path.parent / case["document"]).resolve()
            if not document_path.is_file():
                raise SystemExit(f"{path}: document {document_path} does not exist")
            pdf_bytes = document_path.read_bytes()

        proposal = await client.propose_takeoff(
            TakeoffRequest(
                # A synthetic id: the corpus is files on disk, not rows in a
                # tenant's database, and provenance only has to be stable
                # within one scoring run.
                document_id=uuid.uuid5(uuid.NAMESPACE_URL, str(path)),
                pdf_bytes=pdf_bytes,
                sheet_images=[],
                catalog=catalog,
            )
        )

        scores.append(
            score_case(
                name=case["name"],
                proposal=proposal,
                expected=[
                    ExpectedLine(
                        cost_catalog_item_id=uuid.UUID(line["cost_catalog_item_id"]),
                        quantity=Decimal(str(line["quantity"])),
                    )
                    for line in case["expected"]
                ],
            )
        )

    return CorpusScore(cases=scores)


def _report(score: CorpusScore, minimum: Decimal, provider: str | None) -> None:
    print(f"Provider: {provider or 'fake (no --provider given)'}")
    print(f"Cases:    {len(score.cases)}")
    print()
    for case in score.cases:
        print(f"  {case.name}")
        print(
            f"    matched {len(case.matched)}  missed {len(case.missed)}  "
            f"spurious {len(case.spurious)}  unmatched {case.unmatched_count}  "
            f"rejected {case.rejected_count}"
        )
        # Every out-of-tolerance line by name, because an aggregate that says
        # "82%" tells nobody which sheet to go and look at.
        for line in case.matched:
            if not line.within_tolerance:
                print(
                    f"      off by {line.relative_error:+.2%}: item {line.cost_catalog_item_id} "
                    f"expected {line.expected_quantity}, proposed {line.proposed_quantity}"
                )
    print()
    print(f"Acceptance rate: {score.acceptance_rate:.2%}  (bar: {minimum:.2%})")
    print(f"Recall:          {score.recall:.2%}")
    print()
    # Both numbers, always, and never blended: acceptance alone rewards a
    # provider that proposes one line it is sure of and stays quiet.
    if score.clears_bar(minimum_acceptance=minimum):
        print("CLEARS THE BAR on acceptance. Read the recall figure before concluding anything:")
        print("a provider can clear acceptance while finding half the takeoff.")
    else:
        print("DOES NOT CLEAR THE BAR.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True, type=Path, help="Directory of *.json cases")
    parser.add_argument(
        "--provider",
        default=None,
        help="Takeoff provider to score. Omit to run the fake, which is the floor.",
    )
    parser.add_argument(
        "--minimum-acceptance",
        default="0.70",
        help="The bar, as a fraction. The scoping doc proposes 0.70; it is a choice, not a law.",
    )
    args = parser.parse_args()

    if not args.corpus.is_dir():
        raise SystemExit(f"{args.corpus} is not a directory")

    score = asyncio.run(_score_corpus(args.corpus, args.provider))
    minimum = Decimal(args.minimum_acceptance)
    _report(score, minimum, args.provider)
    # Exit code follows the verdict so this can gate a pipeline later.
    sys.exit(0 if score.clears_bar(minimum_acceptance=minimum) else 1)


if __name__ == "__main__":
    main()
