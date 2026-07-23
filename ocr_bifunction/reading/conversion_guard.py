"""Conversion guards — cheap pre-verify + smart post-detection around a heavy document
conversion (Docling), so a partial read NEVER passes for a whole one.

Measured on real documents (2026-07-20): a heavyweight converter can run out of memory
MID-DOCUMENT and drop pages, yet still report an overall EXCELLENT grade — a silent
truncation that reads as full coverage when it is not. Two lightweight guards close that,
with NO extra heavy machinery (no batching, no second pass):

  1. PRE-VERIFY (before the heavy run): read the page count natively (PyMuPDF, milliseconds,
     no OCR). It costs nothing and it is the DENOMINATOR the post-check compares against —
     you cannot detect a missing page without first knowing how many were expected.

  2. SMART DETECTION (after the heavy run): compare the pages the converter actually produced
     to the pages that were expected. A page the converter failed on (an out-of-memory spike)
     leaves NO result entry, so a plain set difference surfaces it precisely — this catches an
     ERROR (a page that crashed) and an INCOMPLETE document (pages never processed) with the
     SAME check. A partial result is routed to a human, never emitted as a clean success.

This module is deliberately converter-agnostic AND document-type-agnostic AND PURE: it takes
page NUMBERS, not a Docling object and not a document category. The failure it guards is a
property of ANY heavy multi-page read — a scanned 3-page attestation as much as a 50-page
procedure — never of one lane, so it must never be filed under one (e.g. "the SOP reader").
The caller (any document reader) supplies `page_count(path)` as the expected set and the
produced page numbers as the actual set.

This is the SAME completeness invariant the CI flow already embodies at a coarser grain — a CI
submission missing a side is reported `incomplete` with `missing: [recto|verso]` (api_maquette).
"Did every expected UNIT produce a result?" is the universal question; the unit is a file for a
CI pair, a page for a multi-page conversion. This module answers it at page granularity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

# Docling's own poor/fair boundary (validated on 8 real documents 2026-07-20): a page whose
# layout reconstruction scores below this reads as "structure not confidently recovered" — the
# FORM signal (distinct from coverage, which is the COMPLETENESS signal). Kept here as the one
# tunable knob; calibrate it on a labelled batch before trusting it as an auto/human threshold.
LAYOUT_SCORE_REVIEW_THRESHOLD = 0.8

# A converter status that is anything but this is not a clean whole-document success.
CONVERSION_STATUS_SUCCESS = "success"


@dataclass
class ConversionAssessment:
    """The verdict on ONE conversion: did every expected page actually come out?

    `complete` is the load-bearing field — True only when every expected page produced a
    result AND the converter status (if provided) is a clean success. `missing_pages` names
    exactly which pages were dropped (the bad_alloc casualties), so the reason is specific,
    never a vague "something failed". A caller routes `complete is False` to human review."""

    expected_pages: int
    processed_pages: int
    missing_pages: list[int]
    status: str | None
    complete: bool
    reasons: list[str] = field(default_factory=list)


def page_count(path: Path) -> int:
    """The PRE-VERIFY denominator: how many pages the document has, read natively in
    milliseconds (no OCR, no layout). This is what the post-check compares the produced
    pages against — the whole detection rests on knowing this number up front."""
    import pymupdf

    document = pymupdf.open(path)
    try:
        return document.page_count
    finally:
        document.close()


def assess_page_coverage(
    expected_page_numbers: Iterable[int],
    processed_page_numbers: Iterable[int],
    *,
    status: str | None = None,
) -> ConversionAssessment:
    """SMART DETECTION: compare the pages produced to the pages expected.

    A page the converter failed on (out-of-memory) leaves no entry among
    `processed_page_numbers`, so the set difference IS the list of dropped pages — this one
    check covers both an ERROR (a page that crashed) and an INCOMPLETE document (pages never
    reached). `status`, when the converter exposes one, is a corroborating signal: a
    `partial_success`/`failure` status marks the read as non-clean even in the (unlikely)
    case every page slipped through with an entry."""
    expected = sorted(set(expected_page_numbers))
    processed = set(processed_page_numbers)
    missing = [page for page in expected if page not in processed]

    reasons: list[str] = []
    if missing:
        reasons.append(
            f"incomplete conversion: {len(missing)} of {len(expected)} page(s) "
            f"produced no result (dropped, likely out-of-memory): pages {missing}"
        )
    if status is not None and status != CONVERSION_STATUS_SUCCESS:
        reasons.append(f"converter status not a clean success: {status!r}")

    complete = not missing and (status is None or status == CONVERSION_STATUS_SUCCESS)
    return ConversionAssessment(
        expected_pages=len(expected),
        processed_pages=len(expected) - len(missing),
        missing_pages=missing,
        status=status,
        complete=complete,
        reasons=reasons,
    )


def low_layout_pages(
    page_layout_scores: dict[int, float],
    *,
    threshold: float = LAYOUT_SCORE_REVIEW_THRESHOLD,
) -> list[int]:
    """The FORM signal (distinct from coverage): the pages whose layout reconstruction scored
    below `threshold` — the wide/dense tables that came out garbled. A page can be PRESENT
    (counted complete) yet low-form; these two signals are reported separately, never merged,
    so a caller can route 'a page is missing' and 'a page is low-confidence' differently."""
    return sorted(
        page for page, score in page_layout_scores.items() if score < threshold
    )
