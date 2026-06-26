"""Stage ① LIRE — read the text out of a document.

The entry stage is NOT "OCR": it is "get the text out of a document by the means
fit for its type". OCR is only ONE backend, for image-only content. Born-digital
PDFs and .docx carry their text natively and never touch an OCR engine.

Every backend yields TextLine geometry, not just a flat string: the bounding box
is the spatial anchor stage ③ rebuilds fields from (e.g. the value to the right of
"NOM / Surname" → the surname). Raw words carry no links; their positions do.

The OcrEngine Protocol is the jettisonable slot from the cadrage: RapidOCR,
Tesseract or granite-docling all plug in behind it, interchangeable. Which engine
gets selected is a function of the regime (API fast-path vs backoffice batch) and
of confidence — that selection is the "pont" of the dual model, decided upstream,
not here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

# A PDF page yielding fewer than this many characters of native text is treated as
# image-only (scanned) and routed to the OCR engine instead of the text layer.
TEXT_LAYER_MINIMUM_CHARACTERS = 10

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


@dataclass
class TextLine:
    """One text region with its geometry — the spatial anchor templates rely on.

    bbox is axis-aligned (x0, y0, x1, y1) in the page/image coordinate system.
    confidence is None for a native text-layer region (exact) or a float for OCR.
    """

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None = None
    page_index: int = 0


@dataclass
class ReadResult:
    """What stage ① hands to stage ② CATÉGORISER.

    `confidence` is the legibility signal the confidence gate routes on:
    None  -> backend gives no score (native text layer = exact, trust it);
    float -> mean OCR score in [0, 1]; a low value is the "douteux → humain" signal.

    `lines` carries the geometry: stage ③ rebuilds "Nom: …, Prénom: …" from the
    boxes, not from the flat `text` (which is only the lines joined for display).
    """

    document_path: Path
    backend_name: str
    text: str = ""
    lines: list[TextLine] = field(default_factory=list)
    confidence: float | None = None
    page_count: int = 0
    character_count: int = 0
    elapsed_seconds: float = 0.0
    needs_ocr: bool = False  # routed to an OCR engine, but none was wired in
    error: str | None = None


@runtime_checkable
class OcrEngine(Protocol):
    """The jettisonable OCR slot. Any engine that turns an image into lines fits."""

    name: str

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        """Return the recognized text lines (with geometry) for one rendered image."""
        ...


def read_document(
    document_path: Path, ocr_engine: OcrEngine | None = None
) -> ReadResult:
    """Route a document to the backend fit for its type and return its text."""
    suffix = document_path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(document_path, ocr_engine)
    if suffix == ".docx":
        return _read_docx(document_path)
    if suffix in IMAGE_SUFFIXES:
        return _read_image(document_path, ocr_engine)
    return ReadResult(
        document_path=document_path,
        backend_name="(unsupported)",
        error=f"unsupported file type: {suffix}",
    )


def _mean_confidence(lines: list[TextLine]) -> float | None:
    scores = [line.confidence for line in lines if line.confidence is not None]
    return sum(scores) / len(scores) if scores else None


def _read_pdf(document_path: Path, ocr_engine: OcrEngine | None) -> ReadResult:
    import pymupdf

    started_at = time.perf_counter()
    page_texts: list[str] = []
    lines: list[TextLine] = []
    ocr_page_count = 0
    has_text_layer = False
    needs_ocr = False

    with pymupdf.open(document_path) as document:
        page_count = document.page_count
        for page_index, page in enumerate(document):
            native_text = page.get_text().strip()
            if len(native_text) >= TEXT_LAYER_MINIMUM_CHARACTERS:
                has_text_layer = True
                page_texts.append(native_text)
                for x0, y0, x1, y1, block_text, _block_no, block_type in page.get_text(
                    "blocks"
                ):
                    cleaned = block_text.strip()
                    if block_type == 0 and cleaned:
                        lines.append(
                            TextLine(cleaned, (x0, y0, x1, y1), None, page_index)
                        )
                continue
            # Image-only page: render it and hand it to the OCR engine, if any.
            if ocr_engine is None:
                needs_ocr = True
                continue
            pixmap = page.get_pixmap(dpi=200)
            page_lines = ocr_engine.recognize(pixmap.tobytes("png"))
            for line in page_lines:
                line.page_index = page_index
            lines.extend(page_lines)
            page_texts.append("\n".join(line.text for line in page_lines))
            ocr_page_count += 1

    combined_text = "\n".join(page_texts)
    backend_name = "pymupdf-text"
    if ocr_page_count:
        backend_name = (
            f"pymupdf+{ocr_engine.name}" if has_text_layer else ocr_engine.name
        )
    return ReadResult(
        document_path=document_path,
        backend_name=backend_name,
        text=combined_text,
        lines=lines,
        confidence=_mean_confidence(lines),
        page_count=page_count,
        character_count=len(combined_text),
        elapsed_seconds=time.perf_counter() - started_at,
        needs_ocr=needs_ocr,
    )


def _read_docx(document_path: Path) -> ReadResult:
    from docx import Document

    started_at = time.perf_counter()
    document = Document(str(document_path))
    blocks = [
        paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()
    ]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                blocks.append(" | ".join(cells))
    combined_text = "\n".join(blocks)
    # .docx is reflowable: it has no page geometry, so no TextLine boxes here.
    return ReadResult(
        document_path=document_path,
        backend_name="python-docx",
        text=combined_text,
        confidence=None,
        page_count=1,
        character_count=len(combined_text),
        elapsed_seconds=time.perf_counter() - started_at,
    )


def _read_image(document_path: Path, ocr_engine: OcrEngine | None) -> ReadResult:
    if ocr_engine is None:
        return ReadResult(
            document_path=document_path,
            backend_name="(ocr pending)",
            needs_ocr=True,
        )
    started_at = time.perf_counter()
    lines = ocr_engine.recognize(document_path.read_bytes())
    combined_text = "\n".join(line.text for line in lines)
    return ReadResult(
        document_path=document_path,
        backend_name=ocr_engine.name,
        text=combined_text,
        lines=lines,
        confidence=_mean_confidence(lines),
        page_count=1,
        character_count=len(combined_text),
        elapsed_seconds=time.perf_counter() - started_at,
    )
