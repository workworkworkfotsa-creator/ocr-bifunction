"""Docling adapter for the resilient conversion core — the ONLY impure piece.

It implements `PageRangeConverter` by driving Docling's whole-PDF pipeline over ONE absolute page
range at a time (`convert(path, page_range=(start, end))`), and SURFACES the `ConversionResult`
that `DoclingOcrEngine` deliberately discards: which pages actually came out, each page's
`layout_score`, the conversion status, and the rich per-page reading-order markdown. Feeding that to
`reconcile_page_range_conversion` gives the split -> decreasing-backoff -> reconcile behaviour with
Docling as the (interchangeable) engine.

This is a DIFFERENT path from the per-image `DoclingOcrEngine.recognize` — that one converts a
single rendered PNG and drops everything but the text lines; this one keeps whole-document layout
and, crucially, the page-coverage signals the memory-drop failure needs. The per-image engine is
untouched. Verified against Docling 2.107.0 (`export_to_markdown(page_no=…)`, `confidence.pages`,
`document.pages`, `ConversionStatus`)."""

from __future__ import annotations

from pathlib import Path

from ocr_bifunction.conversion_guard import CONVERSION_STATUS_SUCCESS, page_count
from ocr_bifunction.resilient_conversion import (
    CONVERSION_STATUS_FAILURE,
    CONVERSION_STATUS_PARTIAL_SUCCESS,
    DEFAULT_BATCH_SIZE_SCHEDULE,
    PageRangeConversionAttempt,
    PageRangeConverter,
    ResilientConversion,
    TextSpan,
    reconcile_page_range_conversion,
)


def _map_conversion_status(status_name: str) -> str:
    """Map Docling's `ConversionStatus` name onto the guard's status vocabulary. Anything that is
    not a clean SUCCESS or a PARTIAL_SUCCESS (FAILURE, PENDING, STARTED, SKIPPED) is a failure for
    our purposes — but note coverage, not this flag, is what actually drives the retry decision."""
    if status_name == "SUCCESS":
        return CONVERSION_STATUS_SUCCESS
    if status_name == "PARTIAL_SUCCESS":
        return CONVERSION_STATUS_PARTIAL_SUCCESS
    return CONVERSION_STATUS_FAILURE


def _page_text_spans(
    document: object, produced_page_numbers: list[int]
) -> dict[int, list[TextSpan]]:
    """Harvest WHERE each piece of text sat, keyed by absolute page — the provenance the markdown
    alone cannot carry.

    Docling exposes a bbox per item (`item.prov[0]`) and this is the ONLY moment it is available:
    once the page is reduced to markdown the geometry is gone for good, and with it any chance of
    showing a reviewer the region a value came from. Verified 2026-07-21: under `page_range`,
    `prov.page_no` is ABSOLUTE (a chunk (2,3) reports pages 2 and 3), so spans key straight into the
    reconciliation without an offset — the same absolute-numbering property `confidence.pages` has.

    `SectionHeaderItem` subclasses `TextItem` (verified), so headings are harvested too rather than
    silently dropped. The bbox is flipped to the reader's TOP-LEFT origin here, in the adapter, so
    no caller inherits Docling's bottom-left convention."""
    from docling_core.types.doc import TextItem

    wanted = set(produced_page_numbers)
    page_heights = {
        page_number: getattr(getattr(page, "size", None), "height", None)
        for page_number, page in document.pages.items()
    }

    spans: dict[int, list[TextSpan]] = {}
    for element, _level in document.iterate_items():
        if not isinstance(element, TextItem) or not element.text.strip():
            continue
        if not element.prov:
            continue
        provenance = element.prov[0]
        if provenance.page_no not in wanted:
            continue
        bbox = provenance.bbox
        page_height = page_heights.get(provenance.page_no)
        if page_height is not None:
            bbox = bbox.to_top_left_origin(page_height=page_height)
        x0, x1 = min(bbox.l, bbox.r), max(bbox.l, bbox.r)
        y0, y1 = min(bbox.t, bbox.b), max(bbox.t, bbox.b)
        spans.setdefault(provenance.page_no, []).append(
            TextSpan(text=element.text, bbox=(x0, y0, x1, y1))
        )
    return spans


def make_docling_page_range_converter(
    document_path: Path, converter: object | None = None
) -> PageRangeConverter:
    """Build a `PageRangeConverter` that converts one absolute page range of `document_path`.

    `converter` lets a caller inject a pre-built `DocumentConverter` (the heavy model load happens
    once, not per range); when None, one is constructed lazily. The Docling import is deferred so
    importing this module stays cheap and the heavy stack only loads when a conversion actually
    runs (same discipline as `docling_engine.py`)."""
    if converter is None:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()

    def convert_page_range(page_range: tuple[int, int]) -> PageRangeConversionAttempt:
        range_start, range_end = page_range
        result = converter.convert(document_path, page_range=(range_start, range_end))
        # THE produced signal is `confidence.pages`, NOT `document.pages`. Measured on a real
        # 55-page doc (2026-07-20): under memory contention Docling threw std::bad_alloc on the OCR
        # stage for pages 27-55, yet KEPT their structural entry in `document.pages` (so that field
        # reported a false 55/55). A page that fails before scoring is ABSENT from `confidence.pages`
        # — that membership is the honest "this page was actually processed" set, and it is exactly
        # the boundary the backoff must retry. (A scored-but-text-empty front-matter page stays in
        # `confidence.pages`: correctly produced, not a memory casualty.)
        produced_page_numbers = sorted(result.confidence.pages)
        page_layout_scores = {
            page_number: scores.layout_score
            for page_number, scores in result.confidence.pages.items()
        }
        page_markdown = {
            page_number: result.document.export_to_markdown(page_no=page_number)
            for page_number in produced_page_numbers
        }
        return PageRangeConversionAttempt(
            requested_page_range=page_range,
            produced_page_numbers=produced_page_numbers,
            page_markdown=page_markdown,
            page_layout_scores=page_layout_scores,
            status=_map_conversion_status(result.status.name),
            page_text_spans=_page_text_spans(result.document, produced_page_numbers),
        )

    return convert_page_range


def convert_document_resiliently(
    document_path: Path,
    *,
    batch_size_schedule: list[int] = DEFAULT_BATCH_SIZE_SCHEDULE,
    converter: object | None = None,
) -> ResilientConversion:
    """Read a whole multi-page document with Docling, resilient to mid-document memory drops.

    Reads the native page count (the coverage denominator), then drives the pure core over a Docling
    page-range converter. Returns the reconciled whole-document result plus its completeness verdict
    — `complete is False` (with `missing_page_numbers` named) is the caller's cue to route the read
    to a human, exactly as an incomplete CI submission is."""
    expected_page_count = page_count(document_path)
    page_range_converter = make_docling_page_range_converter(document_path, converter)
    return reconcile_page_range_conversion(
        expected_page_count,
        page_range_converter,
        batch_size_schedule=batch_size_schedule,
    )
