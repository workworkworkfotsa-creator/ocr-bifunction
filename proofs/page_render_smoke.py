"""Page-render smoke — the server-side page image the highlight overlay needs.

    uv run python proofs/page_render_smoke.py

WHY THE ENDPOINT EXISTS. The review page used `<embed>` for PDFs, i.e. the browser's built-in
PDF viewer: an opaque plugin. Nothing can be drawn on top of it, its scroll and zoom are
unknown, and the page it displays cannot be chosen — so "show me the zone on page 12" was
impossible for exactly the documents where provenance exists (born-digital PDFs). Rendering the
page ourselves turns every document type into an `<img>`, and the overlay becomes uniform.

PII-free: the corpus is a synthetic multi-page PDF written here.

Proves:
  1. a PDF page renders to a real PNG (magic bytes + plausible size for the requested dpi);
  2. `page` selects the page — two different pages give two DIFFERENT images;
  3. an out-of-range page is a 404, not a silent fallback to page 0 (a wrong page under a
     highlight would point the reviewer at the wrong place while looking right);
  4. an IMAGE file is served as-is (it is its own page) and its page 1 is a 404;
  5. an unknown job and an out-of-range file index are 404;
  6. the render preserves the page ASPECT RATIO — the precondition for a normalized span (a
     per-axis fraction) to land on the right spot whatever resolution the page is rendered at.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_page_render_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

import pymupdf  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

from ocr_bifunction.adapters import api_maquette  # noqa: E402
from ocr_bifunction.storage.repository import Job, SqliteRepository  # noqa: E402
from ocr_bifunction.validation.status import STATUS_NEEDS_REVIEW  # noqa: E402

CHECKS: list[tuple[str, bool]] = []
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _write_multipage_pdf(path: Path, page_count: int) -> None:
    """A PDF whose pages differ visibly, so "page N" can be told from "page 0"."""
    document = pymupdf.open()
    for page_number in range(page_count):
        page = document.new_page()
        page.insert_text(
            (72, 100 + 40 * page_number), f"PAGE {page_number}", fontsize=30
        )
    document.save(path)
    document.close()


def _spool_job(repository: SqliteRepository, files: list[Path]) -> int:
    """A needs_review row whose retained bytes are those files — the review-page situation."""
    spool_directory = _SCRATCH / f"spool_job_{len(files)}_{files[0].stem}"
    spool_directory.mkdir(parents=True, exist_ok=True)
    for source in files:
        (spool_directory / source.name).write_bytes(source.read_bytes())
    return repository.save(
        Job(
            source=files[0].name,
            category_lane="structured",
            status=STATUS_NEEDS_REVIEW,
            document_ref=str(spool_directory),
        )
    )


def run() -> int:
    pdf_path = _SCRATCH / "multipage.pdf"
    _write_multipage_pdf(pdf_path, 3)
    image_path = _SCRATCH / "scan.png"
    pixmap = pymupdf.open(pdf_path)[0].get_pixmap(dpi=72)
    pixmap.save(image_path)

    repository = SqliteRepository(os.environ["OCR_STORE_PATH"])
    try:
        pdf_job = _spool_job(repository, [pdf_path])
        image_job = _spool_job(repository, [image_path])
    finally:
        repository.close()

    client = TestClient(api_maquette.app)

    # --- 1. A PDF page renders to a real PNG. -----------------------------------------
    first = client.get(f"/v1/jobs/{pdf_job}/page", params={"page": 0})
    _check(
        "a PDF page renders to a PNG (status, content-type, magic bytes)",
        first.status_code == 200
        and first.headers["content-type"] == "image/png"
        and first.content.startswith(PNG_MAGIC),
    )
    rendered = pymupdf.Pixmap(first.content)
    # A4 at 150 dpi ~ 1240x1754 px; assert the order of magnitude, not the exact pixel.
    _check(
        f"rendered at the declared dpi ({api_maquette.settings.PAGE_RENDER_DPI}): "
        f"{rendered.width}x{rendered.height} px",
        rendered.width > 800 and rendered.height > rendered.width,
    )

    # --- 2. `page` really selects the page. -------------------------------------------
    second = client.get(f"/v1/jobs/{pdf_job}/page", params={"page": 2})
    _check(
        "page 2 differs from page 0 (the parameter selects, it does not decorate)",
        second.status_code == 200 and second.content != first.content,
    )

    # --- 3. Out-of-range page is a 404, never a silent page 0. -------------------------
    # A wrong page under a highlight looks perfectly right while pointing at the wrong place.
    beyond = client.get(f"/v1/jobs/{pdf_job}/page", params={"page": 99})
    _check(
        "page beyond the document -> 404, not a silent fallback to page 0",
        beyond.status_code == 404,
    )

    # --- 4. An image is its own page. -------------------------------------------------
    image_page = client.get(f"/v1/jobs/{image_job}/page")
    _check(
        "an image file is served as-is for page 0",
        image_page.status_code == 200 and image_page.content == image_path.read_bytes(),
    )
    _check(
        "an image has no page 1 -> 404",
        client.get(f"/v1/jobs/{image_job}/page", params={"page": 1}).status_code == 404,
    )

    # --- 5. Unknown job / file index. -------------------------------------------------
    _check(
        "unknown job -> 404",
        client.get("/v1/jobs/999999/page").status_code == 404,
    )
    _check(
        "file index beyond the retained files -> 404",
        client.get(f"/v1/jobs/{pdf_job}/page", params={"index": 7}).status_code == 404,
    )

    # --- 6. The overlay is resolution-independent, which is the point of normalizing. --
    # The same normalized span lands on the same RELATIVE position whatever the render dpi,
    # so the review page never needs to know the resolution it is displaying.
    page_rectangle = pymupdf.open(pdf_path)[0].rect
    page_ratio = page_rectangle.width / page_rectangle.height
    low = pymupdf.open(pdf_path)[0].get_pixmap(dpi=72)
    high = pymupdf.open(pdf_path)[0].get_pixmap(dpi=300)
    _check(
        "the render preserves the page aspect ratio at 72 and 300 dpi — the precondition "
        "for a normalized span to land on the same spot at any resolution",
        low.width != high.width
        and abs(low.width / low.height - page_ratio) < 0.01
        and abs(high.width / high.height - page_ratio) < 0.01,
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT server-rendered page (the overlay's canvas): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
