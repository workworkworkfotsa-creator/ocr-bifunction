"""Image-dominance smoke — a page whose CONTENT is in its images stops being read blind.

    uv run python image_dominance_smoke.py

THE BUG THIS CLOSES, measured on a real document. `read_document` sent a PDF page to the text
layer as soon as it held 10 native characters. A photo page with a caption clears that bar, so
its images were never read — and the read reported success. A 24-page photo book came out as
6 218 characters of captions, `needs_ocr` False, no error. Same failure class as the Docling
page drop already on record: an incomplete read passing for a whole one.

The page is now OCR'd IN ADDITION when it is image-DOMINANT (images cover > 80 % of the page
AND under 600 native characters), so the exact captions are kept AND the image content is
added, rather than trading one for the other.

Uses a FAKE OcrEngine: deterministic, instant, and no model is loaded — nothing competes for
RAM on the shared machine. PII-free (synthetic pages built here).

Proves:
  1. an image-dominant page gets OCR IN ADDITION — the native caption SURVIVES and the image
     text is appended (the whole point of "in addition" rather than "instead");
  2. the false-positive case is excluded: a page fully covered by a background image but
     carrying a REAL text layer is NOT sent to OCR (7 such pages exist in the real corpus);
  3. a plain text page is untouched — no OCR, backend stays pymupdf-text;
  4. ONE coordinate system per page: the OCR lines are rescaled from render pixels into the
     page's points, so the template geometry rules never compare 0..1654 against 0..595;
  5. no OCR engine wired + a dominant page -> `needs_ocr` True: content we know we cannot read
     is DECLARED, never silently dropped.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf

from ocr_bifunction.reader import (
    IMAGE_DOMINANT_MAXIMUM_CHARACTERS,
    OCR_RENDER_DPI,
    TextLine,
    read_document,
)

CHECKS: list[tuple[str, bool]] = []
_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_image_dominance_smoke_"))
OCR_MARKER = "TEXTE VISIBLE DANS L IMAGE"
CAPTION = "Legende de la photo"


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


class _FakeOcrEngine:
    """Returns one line, positioned in PIXELS of the image it is given — like a real engine."""

    name = "fake-ocr"

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        pixmap = pymupdf.Pixmap(image_png_bytes)
        return [
            TextLine(
                text=OCR_MARKER,
                bbox=(10.0, 20.0, pixmap.width - 10.0, 60.0),
                confidence=0.9,
                page_width=float(pixmap.width),
                page_height=float(pixmap.height),
            )
        ]


def _build_page(path: Path, *, text: str, with_image: bool) -> None:
    """A page with `text`, optionally under a full-page image (the photo-book shape)."""
    document = pymupdf.open()
    page = document.new_page()
    if with_image:
        photo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 400))
        photo.set_rect(photo.irect, (200, 200, 200))
        page.insert_image(page.rect, pixmap=photo)
    # insert_textbox, not insert_text: the latter writes ONE line that runs off the page, so a
    # long string lands as ~130 extracted characters. The fixture must really carry the text
    # layer it claims to, or the false-positive case tests nothing (caught by this smoke).
    page.insert_textbox(page.rect + (40, 40, -40, -40), text, fontsize=9)
    document.save(path)
    document.close()


def run() -> int:
    engine = _FakeOcrEngine()

    # --- 1. Image-dominant: OCR IN ADDITION, the caption survives. ---------------------
    dominant = _SCRATCH / "photo_with_caption.pdf"
    _build_page(dominant, text=CAPTION, with_image=True)
    read = read_document(dominant, engine)
    _check(
        "an image-dominant page is OCR'd, and the native caption SURVIVES alongside",
        CAPTION in read.text and OCR_MARKER in read.text,
    )
    _check(
        "the backend names both sources honestly (pymupdf+<engine>)",
        read.backend_name == f"pymupdf+{engine.name}",
    )

    # --- 4. One coordinate system per page. -------------------------------------------
    with pymupdf.open(dominant) as document:
        page_rect = document[0].rect
    ocr_lines = [line for line in read.lines if line.confidence is not None]
    native_lines = [line for line in read.lines if line.confidence is None]
    _check(
        "both kinds of line are present on the same page",
        bool(ocr_lines) and bool(native_lines),
    )
    _check(
        "the OCR line is rescaled into the page's POINTS, not left in render pixels "
        f"(would be ~{page_rect.width * OCR_RENDER_DPI / 72:.0f} wide)",
        all(
            abs(line.page_width - page_rect.width) < 0.01
            and line.bbox[2] <= page_rect.width + 0.01
            for line in ocr_lines
        ),
    )

    # --- 2. The false positive the real corpus contains: image + REAL text. -----------
    long_text = "Paragraphe de contenu reel. " * 30  # > 600 characters
    covered_but_textual = _SCRATCH / "background_image_with_text.pdf"
    _build_page(covered_but_textual, text=long_text, with_image=True)
    textual = read_document(covered_but_textual, engine)
    _check(
        f"a fully covered page carrying a REAL text layer (> {IMAGE_DOMINANT_MAXIMUM_CHARACTERS} "
        "chars) is NOT sent to OCR",
        OCR_MARKER not in textual.text and textual.backend_name == "pymupdf-text",
    )

    # --- 3. A plain text page is untouched. -------------------------------------------
    plain = _SCRATCH / "plain_text.pdf"
    _build_page(plain, text=CAPTION, with_image=False)
    plain_read = read_document(plain, engine)
    _check(
        "a page without images is untouched (no OCR, backend unchanged)",
        OCR_MARKER not in plain_read.text and plain_read.backend_name == "pymupdf-text",
    )

    # --- 5. No engine: the gap is DECLARED. -------------------------------------------
    without_engine = read_document(dominant, None)
    _check(
        "no OCR engine + a dominant page -> needs_ocr True (content we cannot read is declared)",
        without_engine.needs_ocr is True and CAPTION in without_engine.text,
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT image-dominant pages read instead of skipped: "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
