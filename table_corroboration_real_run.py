"""Real table-corroboration run — Docling vs markitdown on the SAME documents.

    uv run python table_corroboration_real_run.py                  # dry: what it WOULD do
    uv run python table_corroboration_real_run.py --go             # actually runs Docling
    uv run python table_corroboration_real_run.py --go --limit 3 --names

The question this answers on REAL documents: when two UNRELATED table reconstructions are put side
by side — Docling's neural TableFormer against pdfplumber's geometric one — do they agree? Agreement
is genuine evidence (nothing is shared between the methods); disagreement points at the measured weak
spot, wide/dense tables coming out garbled.

TWO PRECAUTIONS BUILT IN:

  1. markitdown is CHEAP and runs FIRST, as a selector: only documents it finds tables in are sent to
     the expensive Docling pass. No heavy conversion is spent on a document with nothing to compare.
  2. COMPLETENESS IS REPORTED ALONGSIDE. Docling can drop pages under memory contention; a document
     read short will show FEWER tables for that reason, which is a coverage failure, not a
     table-extraction failure. Conflating the two would be the analysis trap of this run, so an
     incomplete read is labelled and its divergence is NOT counted as a table disagreement.

SHARED MACHINE (non-negotiable): the Docling pass competes for RAM with the other projects on this
box. `--go` is required, and only when nothing else heavy is running. A bad_alloc mid-run is the
machine under contention, not a code bug — the resilient converter is there to survive it.

PII: prints ONLY shapes and counts (table counts, rows x columns, page numbers, timing). It NEVER
prints cell content. File names are redacted unless `--names` is passed (local use only).

This script MEASURES; the interpretation and the decision are the user's.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from ocr_bifunction.table_corroboration import (
    TableProfile,
    compare_table_profiles,
    extract_table_profiles,
)

DEFAULT_INPUT_DIRECTORY = Path("inputs")


def _display_name(path: Path, index: int, *, show_real_names: bool) -> str:
    if show_real_names:
        return path.name
    return f"{path.parent.name}/doc{index:02d}"


def _shapes(profiles: list[TableProfile]) -> str:
    return ", ".join(f"{p.row_count}x{p.column_count}" for p in profiles) or "-"


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="*", default=[str(DEFAULT_INPUT_DIRECTORY)])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--names", action="store_true")
    parser.add_argument(
        "--go", action="store_true", help="required: runs heavy Docling"
    )
    arguments = parser.parse_args()

    from markitdown import MarkItDown

    documents: list[Path] = []
    for raw in arguments.inputs:
        candidate = Path(raw)
        if candidate.is_dir():
            documents.extend(sorted(p for p in candidate.rglob("*.pdf") if p.is_file()))
        elif candidate.is_file():
            documents.append(candidate)

    # PASS 1 — cheap selector: markitdown decides which documents are worth the heavy pass.
    print(
        "Pass 1 (cheap): markitdown selects documents that actually contain tables.\n"
    )
    markitdown_reader = MarkItDown()
    selected: list[tuple[Path, int, list[TableProfile]]] = []
    for index, path in enumerate(documents, start=1):
        try:
            markdown = markitdown_reader.convert(path).text_content
        except (
            Exception
        ) as error:  # a reader that cannot open the file is not a comparison
            print(
                f"  skip {_display_name(path, index, show_real_names=arguments.names)}"
                f" — markitdown error {type(error).__name__}"
            )
            continue
        profiles = extract_table_profiles(markdown)
        if profiles:
            selected.append((path, index, profiles))

    if arguments.limit is not None:
        selected = selected[: arguments.limit]

    print(
        f"  {len(selected)} of {len(documents)} document(s) carry tables -> heavy pass.\n"
    )
    if not selected:
        print("Nothing to compare.")
        return 0

    if not arguments.go:
        print("DRY RUN — would run Docling on:")
        for path, index, profiles in selected:
            name = _display_name(path, index, show_real_names=arguments.names)
            print(
                f"  {name:<22} markitdown: {len(profiles)} table(s)  [{_shapes(profiles)}]"
            )
        print("\nRe-run with --go to execute (heavy: shared machine).")
        return 0

    # PASS 2 — the heavy side. One converter, reused: the model load is the big fixed cost.
    from docling.document_converter import DocumentConverter

    from ocr_bifunction.docling_page_range_converter import convert_document_resiliently

    print("Pass 2 (heavy): Docling, one shared converter.\n")
    started_all = time.perf_counter()
    converter = DocumentConverter()
    print(f"  converter ready in {time.perf_counter() - started_all:.1f}s\n")

    agreed = diverged = incomplete = 0
    for path, index, markitdown_profiles in selected:
        name = _display_name(path, index, show_real_names=arguments.names)
        started = time.perf_counter()
        conversion = convert_document_resiliently(path, converter=converter)
        elapsed = time.perf_counter() - started

        docling_markdown = "\n\n".join(
            page.markdown for page in conversion.page_results
        )
        docling_profiles = extract_table_profiles(docling_markdown)
        comparison = compare_table_profiles(docling_profiles, markitdown_profiles)

        print(f"  {name}  ({elapsed:.1f}s)")
        print(
            f"    docling    : {len(docling_profiles):>3} table(s)  [{_shapes(docling_profiles)}]"
        )
        print(
            f"    markitdown : {len(markitdown_profiles):>3} table(s)  [{_shapes(markitdown_profiles)}]"
        )

        if not conversion.assessment.complete:
            incomplete += 1
            print(
                f"    -> READ INCOMPLETE: pages {conversion.assessment.missing_pages} never "
                "produced. Fewer tables is EXPECTED here — a coverage failure, NOT a table "
                "disagreement. Not counted as divergence."
            )
        elif comparison.corroborated:
            agreed += 1
            print("    -> CORROBORATED (two unrelated methods, same shapes)")
        else:
            diverged += 1
            for reason in comparison.reasons:
                print(f"    -> DIVERGES: {reason}")
        print()

    print("=" * 78)
    print(
        f"CORROBORATED {agreed} | DIVERGED {diverged} | INCOMPLETE READ {incomplete} "
        f"(of {len(selected)}) in {time.perf_counter() - started_all:.1f}s"
    )
    print(
        "\nA divergence is a POINTER, not a verdict: it says the two methods disagree, not which\n"
        "one is right. Inspect the named shapes before concluding. This script measures; the\n"
        "interpretation and the decision are the user's."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
