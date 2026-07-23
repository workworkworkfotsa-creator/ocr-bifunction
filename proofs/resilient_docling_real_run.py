"""Real Docling run — the EMPIRICAL test of the decreasing-backoff hypothesis.

    uv run python proofs/resilient_docling_real_run.py                 # dry run: lists what it WOULD do
    uv run python proofs/resilient_docling_real_run.py --go            # actually runs Docling
    uv run python proofs/resilient_docling_real_run.py --go --schedule 20,10,5,2,1 --limit 3

The core question this answers on REAL documents (not the pure smoke): when Docling drops a page
under memory contention at a big batch, does a SMALLER batch recover it? A page counts as RECOVERED
only when a RETRY round produced it (`produced_in_round > 0`) — never merely because its round-0
remainder chunk was narrower than the initial batch. If nothing was recovered, the first pass
produced every page and the backoff never had to fire at that initial size. Use the per-round /
per-width breakdown to CALIBRATE `DEFAULT_BATCH_SIZE_SCHEDULE` and the 0.8 layout threshold before
either becomes an auto/human gate. The user decides the retained schedule — this script measures, it
does not verdict.

SHARED MACHINE (non-negotiable): heavy Docling conversion competes for RAM with the other projects
on this box. Only run with `--go`, and only when nothing else heavy is running. Never kill a
llama-server / converter by image name (that hits another project) — stop only this script's own
process. A ConnectionReset / bad_alloc mid-run is the machine under contention, not a code bug (that
is exactly the failure this backoff exists to survive — rerun a smaller schedule).

PII: prints ONLY metadata (page counts, page numbers, layout scores, provenance, timing). It NEVER
prints page content — the reconciled markdown of real contracts/CI carries PII and stays in memory.
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from pathlib import Path

from ocr_bifunction.reading.resilient_conversion import (
    DEFAULT_BATCH_SIZE_SCHEDULE,
    ResilientConversion,
)

DEFAULT_INPUT_DIRECTORIES = [Path("inputs/sop"), Path("inputs/cplx")]


def _parse_schedule(raw: str) -> list[int]:
    return [int(piece) for piece in raw.split(",") if piece.strip()]


def _collect_pdfs(input_directories: list[Path], limit: int | None) -> list[Path]:
    pdfs: list[Path] = []
    for directory in input_directories:
        if directory.is_dir():
            pdfs.extend(sorted(directory.glob("*.pdf")))
        elif directory.suffix.lower() == ".pdf" and directory.is_file():
            pdfs.append(directory)
    return pdfs[:limit] if limit is not None else pdfs


def _report(
    document_path: Path, conversion: ResilientConversion, elapsed: float
) -> None:
    produced_per_round = Counter(
        page.produced_in_round for page in conversion.page_results
    )
    produced_per_width = Counter(
        page.produced_by_batch_size for page in conversion.page_results
    )
    # HONEST recovery = produced on a RETRY round (produced_in_round > 0), NOT merely at a width
    # below the initial batch — the last round-0 chunk is the arithmetic remainder, narrower than
    # the batch yet never a recovery. If this list is empty, the first pass produced every page and
    # the backoff never had to fire (batching alone sufficed at this initial size).
    recovered_by_backoff = sorted(
        page.page_number
        for page in conversion.page_results
        if page.produced_in_round > 0
    )
    verdict = "COMPLETE" if conversion.complete else "INCOMPLETE"
    print(f"\n{document_path.name}")
    print(
        f"  {verdict}  expected={conversion.expected_page_count} "
        f"produced={len(conversion.page_results)} "
        f"attempts={conversion.attempt_count} elapsed={elapsed:.1f}s"
    )
    print(
        "  pages per round (0 = first pass, >0 = backoff retry): "
        + ", ".join(
            f"round{index}:{count}"
            for index, count in sorted(produced_per_round.items())
        )
    )
    print(
        "  pages per batch width: "
        + ", ".join(
            f"{width}px:{count}" for width, count in sorted(produced_per_width.items())
        )
    )
    if recovered_by_backoff:
        print(
            f"  RECOVERED by backoff (dropped at a bigger batch, produced on a retry): "
            f"{recovered_by_backoff}"
        )
    else:
        print(
            "  RECOVERED by backoff: NONE — the first pass produced every page "
            "(the backoff safety net did not need to fire at this initial batch size)"
        )
    if conversion.missing_page_numbers:
        print(
            f"  MISSING (genuinely bad, -> human review): {conversion.missing_page_numbers}"
        )
    if conversion.low_form_page_numbers:
        print(
            f"  LOW-FORM (layout below threshold): {conversion.low_form_page_numbers}"
        )
    if conversion.nonclean_chunk_statuses:
        print(f"  noisy chunk statuses: {conversion.nonclean_chunk_statuses}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--go", action="store_true", help="actually run Docling (heavy)"
    )
    parser.add_argument(
        "--schedule",
        type=str,
        default=",".join(str(size) for size in DEFAULT_BATCH_SIZE_SCHEDULE),
        help="decreasing batch-size schedule, e.g. 20,10,5,2,1",
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="max documents to process"
    )
    parser.add_argument(
        "--inputs",
        nargs="*",
        type=Path,
        default=DEFAULT_INPUT_DIRECTORIES,
        help="directories or PDF files to read",
    )
    arguments = parser.parse_args()

    schedule = _parse_schedule(arguments.schedule)
    pdfs = _collect_pdfs(arguments.inputs, arguments.limit)

    print(f"schedule: {schedule}")
    print(f"documents ({len(pdfs)}):")
    for pdf in pdfs:
        print(f"  {pdf}")

    if not arguments.go:
        print(
            "\nDRY RUN — no Docling launched. Re-run with --go to convert for real "
            "(shared machine: confirm nothing else heavy is running first)."
        )
        return 0
    if not pdfs:
        print("\nNo PDFs found in the given inputs — nothing to run.")
        return 1

    # Heavy imports and the shared DocumentConverter (one model load reused across all docs) only
    # happen here, under --go.
    from docling.document_converter import DocumentConverter

    from ocr_bifunction.reading.docling_page_range_converter import (
        make_docling_page_range_converter,
    )
    from ocr_bifunction.reading.resilient_conversion import (
        reconcile_page_range_conversion,
    )

    from ocr_bifunction.reading.conversion_guard import page_count

    shared_converter = DocumentConverter()
    print("\nDocling converter loaded — running one document at a time.\n")

    complete_count = 0
    for pdf in pdfs:
        started_at = time.perf_counter()
        expected_page_count = page_count(pdf)
        page_range_converter = make_docling_page_range_converter(pdf, shared_converter)
        conversion = reconcile_page_range_conversion(
            expected_page_count, page_range_converter, batch_size_schedule=schedule
        )
        _report(pdf, conversion, time.perf_counter() - started_at)
        complete_count += int(conversion.complete)

    print(
        f"\nSUMMARY: {complete_count}/{len(pdfs)} documents complete "
        f"under schedule {schedule}. Calibrate the schedule from the provenance above; "
        "the retained schedule is the user's call."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
