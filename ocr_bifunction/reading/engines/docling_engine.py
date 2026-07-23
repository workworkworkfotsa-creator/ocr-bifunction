"""Docling — the heavyweight fallback OcrEngine for the batch / escalade lane.

Docling runs full document understanding (layout + OCR + reading order), far heavier
than RapidOCR: torch-based models, CPU-slow on the 8 GB target. Per the cadrage it is
the fallback for hard image/scan pages and the batch-soir / escalade regime, NEVER the
API fast-path. It plugs behind the same jettisonable OcrEngine slot.

We map each recognized TextItem to one TextLine (text + top-left bbox + page) and drop
Docling's richer structure (tables, markdown, reading order) — that richness is for the
RAG lane, not the template geometry. Imports are deferred so the heavy stack only loads
when this engine is actually constructed.
"""

from __future__ import annotations

from io import BytesIO

from ocr_bifunction.reading.reader import TextLine


def _stream_name(image_bytes: bytes) -> str:
    """Name the stream so Docling detects the format — sniff JPEG vs PNG by magic."""
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "page.jpg"
    return "page.png"


class DoclingOcrEngine:
    name = "docling"

    def __init__(self) -> None:
        # First construction loads/downloads the layout + OCR models (slow, CPU).
        from docling.document_converter import DocumentConverter

        self._converter = DocumentConverter()

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        from docling.datamodel.base_models import DocumentStream
        from docling_core.types.doc import TextItem

        stream = DocumentStream(
            name=_stream_name(image_png_bytes), stream=BytesIO(image_png_bytes)
        )
        document = self._converter.convert(stream).document

        # Page heights let us flip Docling's bottom-left bbox origin to our top-left one
        # — essential so the template "value below" geometry keeps the right orientation.
        # Widths serve the other need: the frame of reference provenance normalizes against.
        page_heights = {
            page_no: getattr(getattr(page, "size", None), "height", None)
            for page_no, page in document.pages.items()
        }
        page_widths = {
            page_no: getattr(getattr(page, "size", None), "width", None)
            for page_no, page in document.pages.items()
        }

        lines: list[TextLine] = []
        for element, _level in document.iterate_items():
            if not isinstance(element, TextItem) or not element.text.strip():
                continue
            if not element.prov:
                continue
            provenance = element.prov[0]
            bbox = provenance.bbox
            page_height = page_heights.get(provenance.page_no)
            if page_height is not None:
                bbox = bbox.to_top_left_origin(page_height=page_height)
            x0, x1 = min(bbox.l, bbox.r), max(bbox.l, bbox.r)
            y0, y1 = min(bbox.t, bbox.b), max(bbox.t, bbox.b)
            page_width = page_widths.get(provenance.page_no)
            lines.append(
                TextLine(
                    text=element.text,
                    bbox=(x0, y0, x1, y1),
                    confidence=None,  # Docling exposes no per-line OCR score here
                    page_index=provenance.page_no - 1,
                    # A page whose size Docling did not report stays at 0 = unknown: no
                    # provenance rather than one normalized against a guessed frame.
                    page_width=float(page_width) if page_width else 0.0,
                    page_height=float(page_height) if page_height else 0.0,
                )
            )
        return lines
