"""Conversion-guard smoke — the pre-verify + smart-detection guards (conversion_guard.py).

    uv run python conversion_guard_smoke.py

Runs NO heavy converter (Docling stays untouched here — the guards are converter-agnostic
and pure). Proves, on a synthetic 3-page PDF (PyMuPDF, milliseconds, PII-free):
  1. PRE-VERIFY: page_count reads the real page total natively;
  2. DETECTION, whole document: every expected page produced -> complete;
  3. DETECTION, dropped page: a page with no result (the bad_alloc casualty) -> INCOMPLETE,
     the missing page named exactly, complete is False (never a silent success);
  4. DETECTION, converter status: a 'partial_success' status marks the read non-clean even
     when coverage looks full;
  5. FORM signal: low-layout pages surface separately from coverage (present but garbled).

This is exactly the 21-page/50-page real-document failure reproduced as a pure unit. The guard
is NOT SOP-specific: it protects ANY heavy multi-page read whatever the document type (a scanned
attestation as much as a procedure), which is why the smoke uses a plain synthetic PDF and never
names a lane.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf

from ocr_bifunction.conversion_guard import (
    assess_page_coverage,
    low_layout_pages,
    page_count,
)

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def run() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="ocr_bifunction_guard_smoke_"))
    pdf_path = scratch / "three_pages.pdf"
    document = pymupdf.open()
    for index in range(3):
        page = document.new_page()
        page.insert_text((72, 72), f"Page {index + 1} content.", fontsize=11)
    document.save(pdf_path)
    document.close()

    # 1. PRE-VERIFY: the native page count is the detection denominator.
    expected = page_count(pdf_path)
    _check("pre-verify: page_count reads 3 pages natively", expected == 3)

    expected_pages = range(1, expected + 1)

    # 2. Whole document: every expected page produced -> complete.
    whole = assess_page_coverage(expected_pages, [1, 2, 3], status="success")
    _check(
        "whole document -> complete, no missing pages",
        whole.complete and not whole.missing_pages and whole.processed_pages == 3,
    )

    # 3. A dropped page (the bad_alloc casualty) -> INCOMPLETE, named, never silent.
    dropped = assess_page_coverage(expected_pages, [1, 3], status="success")
    _check(
        "dropped page -> incomplete, page 2 named, complete is False",
        not dropped.complete
        and dropped.missing_pages == [2]
        and dropped.processed_pages == 2
        and any("page" in reason for reason in dropped.reasons),
    )

    # 4. Converter status corroborates: partial_success is not a clean success even if
    #    every page happened to carry an entry.
    partial = assess_page_coverage(expected_pages, [1, 2, 3], status="partial_success")
    _check(
        "partial_success status -> not complete even with full coverage",
        not partial.complete and any("status" in reason for reason in partial.reasons),
    )

    # 5. FORM signal is separate from coverage: a present-but-garbled page surfaces on its own.
    low_form = low_layout_pages({1: 0.95, 2: 0.60, 3: 0.88})
    _check(
        "form signal: low-layout page 2 surfaces below the 0.8 threshold",
        low_form == [2],
    )

    # A clean sheet: no page below threshold -> nothing flagged.
    clean_form = low_layout_pages({1: 0.95, 2: 0.91, 3: 0.88})
    _check(
        "form signal: all pages above threshold -> nothing flagged", clean_form == []
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT conversion guards (pre-verify + detection): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
