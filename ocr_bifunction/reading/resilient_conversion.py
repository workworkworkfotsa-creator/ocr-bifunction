"""Resilient multi-page conversion — split into page-range batches, retry any dropped page
on a SMALLER batch, then reconcile the partial results into one whole document.

Measured on real documents (2026-07-20, HANDOFF): a heavyweight converter (Docling) can run
out of memory MID-DOCUMENT and drop pages while still reporting an overall EXCELLENT grade.
The cause is memory ACCUMULATION within a single conversion on a shared machine — the very
pages that crash in a full run come out impeccable in a FRESH, SMALLER batch. The content is
fine; the batch was too big.

`conversion_guard` DETECTS that (native page_count vs produced pages). This module is the
ACTION built on top of it: convert in batches, and on any page that dropped, replay it under a
progressively smaller batch size drawn from a DECREASING SCHEDULE (e.g. 20 -> 10 -> 5 -> 2 -> 1),
reconciling the produced pages by ABSOLUTE page number. A page that is STILL missing after the
smallest batch (1) is genuinely bad content, not a memory casualty, and is routed to a human —
the same completeness invariant the CI flow embodies (`incomplete` / `missing: [recto|verso]`),
here at page granularity.

The orchestration is PURE and converter-agnostic (like `conversion_guard`): it drives a
`PageRangeConverter` — any callable that converts one absolute page range — so the whole
split/retry/reconcile can be proven with a fake converter WITHOUT running Docling (the shared
machine is preserved). The Docling adapter is the only impure piece, and it lives elsewhere.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ocr_bifunction.reading.conversion_guard import (
    CONVERSION_STATUS_SUCCESS,
    ConversionAssessment,
    assess_page_coverage,
    low_layout_pages,
)

# The status vocabulary a converter attempt reports (the guard already owns "success"). A page
# range that came out clean is "success"; anything else corroborates a non-clean read but does
# NOT by itself block completeness — coverage is the load-bearing signal, form/status is separate.
CONVERSION_STATUS_PARTIAL_SUCCESS = "partial_success"
CONVERSION_STATUS_FAILURE = "failure"

# The backoff levier: batch sizes tried in order, each pass narrower than the last. This is a
# STARTING GUESS for the ~8 GB target — calibrate it on a labelled batch (the real Docling run is
# exactly the experiment that measures which sizes recover which pages). The schedule MUST end at
# 1 so every page gets a final page-by-page attempt before being condemned as genuinely bad.
DEFAULT_BATCH_SIZE_SCHEDULE = [16, 8, 4, 2, 1]


@dataclass(frozen=True)
class TextSpan:
    """One positioned piece of text — the PROVENANCE unit the reconciliation carries through.

    Deliberately NOT a reader type: this core stays free of `TextLine` (which lives in `reader.py`
    and imports THIS module, so depending on it would be circular). The caller rebuilds its own line
    type from these spans. `bbox` is `(x0, y0, x1, y1)` in a TOP-LEFT origin, matching the reader's
    contract, so the adapter — not the caller — owns the flip from a converter's own convention.

    Why it rides along at all: a heavy converter knows WHERE each piece of text sat on the page, and
    that is the only moment it is known. Dropped here, it cannot be recovered from the markdown
    downstream — and without it a reviewer can never be shown the region a value came from."""

    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class PageRangeConversionAttempt:
    """What ONE conversion of a single page range produced.

    All page numbers are ABSOLUTE 1-based — the whole reconciliation rests on the converter
    keeping the original page numbering (Docling does: a chunk (5, 8) yields pages 5..8, not
    1..4). A page the converter crashed on leaves NO entry in `produced_page_numbers`, so a
    plain set difference against the requested range surfaces exactly which pages dropped."""

    requested_page_range: tuple[int, int]  # (start, end), 1-based inclusive
    produced_page_numbers: list[int]  # the pages that actually came out
    page_markdown: dict[int, str]  # absolute page number -> rich reading-order markdown
    page_layout_scores: dict[
        int, float
    ]  # absolute page number -> layout_score in [0, 1]
    status: str  # CONVERSION_STATUS_SUCCESS | _PARTIAL_SUCCESS | _FAILURE
    # absolute page number -> the positioned text of that page. Empty when a converter exposes no
    # geometry (the contract stays satisfiable by a text-only converter, e.g. the smoke's fake one).
    page_text_spans: dict[int, list[TextSpan]] = field(default_factory=dict)
    # absolute page number -> (width, height) IN THE SAME UNIT as the spans' bbox. Without it a
    # span is unplaceable (see reader.ProvenanceSpan), so it travels WITH the geometry or not at
    # all — a converter exposing spans but no sizes simply yields no provenance downstream.
    page_sizes: dict[int, tuple[float, float]] = field(default_factory=dict)


@runtime_checkable
class PageRangeConverter(Protocol):
    """Any callable that converts ONE absolute page range and reports what came out.

    This is the seam that keeps the split/retry/reconcile pure: the fake converter in the smoke
    and the real Docling adapter both satisfy it, so the algorithm is proven without touching the
    shared machine."""

    def __call__(self, page_range: tuple[int, int]) -> PageRangeConversionAttempt: ...


@dataclass
class PageResult:
    """One reconciled page in the final disjoint union of the whole document."""

    page_number: int  # absolute 1-based
    markdown: str  # the rich content that won for this page
    layout_score: float
    produced_by_batch_size: (
        int  # PROVENANCE: width of the batch that finally produced this page
    )
    produced_in_round: int = (
        0  # 0 = produced on the first (largest-batch) pass; > 0 = RECOVERED by the backoff on
        # a later, smaller-batch round. This — not the width — is the honest "did a retry save
        # this page?" signal (a round-0 remainder chunk is narrower than the batch yet is NOT a
        # recovery).
    )
    # The page's positioned text, carried from the attempt that won this page. Empty when the
    # converter exposes no geometry — never silently fabricated.
    text_spans: list[TextSpan] = field(default_factory=list)
    # The page's size in the spans' unit; 0 = the converter did not report it (-> no provenance).
    page_width: float = 0.0
    page_height: float = 0.0


@dataclass
class ResilientConversion:
    """The whole-document reconciled result plus its completeness verdict.

    `assessment` (from conversion_guard, reused verbatim) is the single source of truth for
    completeness; `complete` and `missing_page_numbers` mirror it for convenience. `page_results`
    is the disjoint union ordered by page number — the original reading order across all batches."""

    expected_page_count: int
    page_results: list[PageResult]
    missing_page_numbers: list[int]  # genuinely bad: still absent after the size-1 pass
    low_form_page_numbers: list[
        int
    ]  # present but layout_score below threshold (never retried)
    nonclean_chunk_statuses: list[
        tuple[tuple[int, int], str]
    ]  # observability: noisy attempts
    assessment: ConversionAssessment
    complete: bool
    batch_size_schedule: list[int]
    attempt_count: int = 0


def _validate_batch_size_schedule(batch_size_schedule: list[int]) -> None:
    """Fail LOUD on a malformed levier — a silently-corrected schedule would mask a config bug
    and could loop forever. Required: non-empty, positive, strictly decreasing, ending at 1."""
    if not batch_size_schedule:
        raise ValueError("batch_size_schedule must not be empty")
    if any(batch_size <= 0 for batch_size in batch_size_schedule):
        raise ValueError(
            f"batch_size_schedule must be positive integers: {batch_size_schedule}"
        )
    strictly_decreasing = all(
        earlier > later
        for earlier, later in zip(batch_size_schedule, batch_size_schedule[1:])
    )
    if not strictly_decreasing:
        raise ValueError(
            f"batch_size_schedule must be strictly decreasing: {batch_size_schedule}"
        )
    if batch_size_schedule[-1] != 1:
        raise ValueError(
            "batch_size_schedule must end at 1 so every page gets a final page-by-page "
            f"attempt before being condemned: {batch_size_schedule}"
        )


def _contiguous_runs(page_numbers: Iterable[int]) -> list[tuple[int, int]]:
    """Group page numbers into maximal consecutive (start, end) runs — this is what confines a
    retry pass to the missing pages ONLY (never re-touching an already-produced page)."""
    ordered = sorted(set(page_numbers))
    runs: list[tuple[int, int]] = []
    for page_number in ordered:
        if runs and page_number == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], page_number)
        else:
            runs.append((page_number, page_number))
    return runs


def _contiguous_chunks(
    range_start: int, range_end: int, batch_size: int
) -> list[tuple[int, int]]:
    """Partition [range_start..range_end] into contiguous ranges of AT MOST `batch_size` pages.
    `batch_size` is a ceiling, not an exact width: a run shorter than it is converted whole."""
    chunks: list[tuple[int, int]] = []
    chunk_start = range_start
    while chunk_start <= range_end:
        chunk_end = min(chunk_start + batch_size - 1, range_end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + 1
    return chunks


def reconcile_page_range_conversion(
    expected_page_count: int,
    page_range_converter: PageRangeConverter,
    *,
    batch_size_schedule: list[int] = DEFAULT_BATCH_SIZE_SCHEDULE,
) -> ResilientConversion:
    """Convert a document in decreasing batch sizes until every page is produced or condemned.

    Round 0 partitions the whole document into batches of the LARGEST schedule size. Each later
    round collects the pages STILL missing, regroups them into contiguous runs, and reconverts
    each run at the NEXT-SMALLER schedule size — a fresh, smaller conversion whose memory does not
    accumulate across the pages that dropped. Produced pages accumulate into a disjoint union keyed
    by absolute page number; pages absent after the size-1 pass are genuinely bad and named for
    human review (the completeness gate is `conversion_guard.assess_page_coverage`, reused as-is)."""
    _validate_batch_size_schedule(batch_size_schedule)
    if expected_page_count < 1:
        raise ValueError(f"expected_page_count must be >= 1: {expected_page_count}")

    all_page_numbers = set(range(1, expected_page_count + 1))
    accumulated_page_results: dict[int, PageResult] = {}
    nonclean_chunk_statuses: list[tuple[tuple[int, int], str]] = []
    attempt_count = 0

    for round_index, batch_size in enumerate(batch_size_schedule):
        pages_still_missing = sorted(all_page_numbers - accumulated_page_results.keys())
        if not pages_still_missing:
            break  # every page is produced; the smaller passes have nothing left to recover
        for run_start, run_end in _contiguous_runs(pages_still_missing):
            for chunk_start, chunk_end in _contiguous_chunks(
                run_start, run_end, batch_size
            ):
                attempt = page_range_converter((chunk_start, chunk_end))
                attempt_count += 1
                chunk_width = chunk_end - chunk_start + 1
                expected_here = set(range(chunk_start, chunk_end + 1))
                produced_here = set(attempt.produced_page_numbers) & expected_here

                for page_number in produced_here:
                    candidate = PageResult(
                        page_number=page_number,
                        markdown=attempt.page_markdown[page_number],
                        layout_score=attempt.page_layout_scores.get(page_number, 1.0),
                        produced_by_batch_size=chunk_width,
                        produced_in_round=round_index,
                        text_spans=attempt.page_text_spans.get(page_number, []),
                        page_width=attempt.page_sizes.get(page_number, (0.0, 0.0))[0],
                        page_height=attempt.page_sizes.get(page_number, (0.0, 0.0))[1],
                    )
                    existing = accumulated_page_results.get(page_number)
                    # By construction a retried run holds only previously-missing pages, so this is
                    # a first insert; the keep-smaller-batch guard is defensive (a smaller, fresher
                    # read is more trustworthy) and mirrors pipeline._better_read's keep-best.
                    if (
                        existing is None
                        or chunk_width < existing.produced_by_batch_size
                    ):
                        accumulated_page_results[page_number] = candidate

                if attempt.status != CONVERSION_STATUS_SUCCESS:
                    nonclean_chunk_statuses.append(
                        ((chunk_start, chunk_end), attempt.status)
                    )

    produced_page_numbers = set(accumulated_page_results)
    missing_page_numbers = sorted(all_page_numbers - produced_page_numbers)
    whole_document_status = (
        CONVERSION_STATUS_SUCCESS
        if not missing_page_numbers
        else CONVERSION_STATUS_PARTIAL_SUCCESS
    )
    assessment = assess_page_coverage(
        range(1, expected_page_count + 1),
        produced_page_numbers,
        status=whole_document_status,
    )
    # FORM signal, computed over produced pages only and kept SEPARATE from coverage: a low-form
    # page is still complete (never retried — form is content, not a batch-size problem).
    low_form_page_numbers = low_layout_pages(
        {page: result.layout_score for page, result in accumulated_page_results.items()}
    )

    return ResilientConversion(
        expected_page_count=expected_page_count,
        page_results=[
            accumulated_page_results[page_number]
            for page_number in sorted(accumulated_page_results)
        ],
        missing_page_numbers=missing_page_numbers,
        low_form_page_numbers=low_form_page_numbers,
        nonclean_chunk_statuses=nonclean_chunk_statuses,
        assessment=assessment,
        complete=assessment.complete,
        batch_size_schedule=list(batch_size_schedule),
        attempt_count=attempt_count,
    )
