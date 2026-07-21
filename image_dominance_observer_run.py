"""Image-dominance observer — how many pages does the text-layer gate read BLIND?

    uv run python image_dominance_observer_run.py
    uv run python image_dominance_observer_run.py --inputs inputs/SOP --names

WHAT THIS ANSWERS. `reader.read_document` sends a PDF page to the text layer as soon as it holds
`TEXT_LAYER_MINIMUM_CHARACTERS` (10) native characters, and to an OCR engine otherwise. That gate
asks "does this page have ANY text?" when the question is "is this page's CONTENT in its text
layer?". A photo page with a caption answers yes to the first and no to the second: 14 characters
are enough to declare a full-page photo born-digital, its images are never read, and the read
reports success — 24/24 pages of a real photo book, measured 2026-07-21. Same failure class as the
Docling drop already on record: an incomplete read passing for a whole one. The page-grain
completeness guard cannot see it either, since every page IS produced.

The signal needs no model: PyMuPDF gives image rectangles and native text for free, so a page can
be described by IMAGE COVERAGE (share of the page area under images) and TEXT DENSITY (native
characters). This pass measures both across the corpus and reports the distribution.

It MEASURES, it does not verdict and it changes NOTHING: the threshold, and whether to wire one at
all, is the user's call. Deliberately run BEFORE any wiring — the lesson of the table-shape metric,
which was green on its smoke and died on its first real run because it had been designed against a
single case.

LIGHT: no OCR engine, no heavy converter. Only PyMuPDF geometry and native text — milliseconds per
page, nothing competing for RAM on the shared machine.

PII: prints ONLY counts and percentages, never document content. File NAMES are redacted by
default (this walks all of `inputs/`, where a scan's filename can carry a person's name); `--names`
is for a local run, never paste that output anywhere.
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from ocr_bifunction.reader import TEXT_LAYER_MINIMUM_CHARACTERS

DEFAULT_INPUT_DIRECTORY = Path(__file__).parent / "inputs"

# Candidate thresholds, reported as a WHAT-IF only — nothing consumes them.
CANDIDATE_COVERAGE_PERCENT = 80.0
CANDIDATE_MAXIMUM_CHARACTERS = 600


def _label(document_path: Path, index: int, show_real_names: bool) -> str:
    if show_real_names:
        return document_path.name
    return f"{document_path.parent.name}/doc{index:02d}{document_path.suffix.lower()}"


def _page_measurements(page) -> tuple[int, float]:
    """(native characters, image coverage as a % of page area) for one page.

    Coverage can exceed 100 %: images overlap and stack (a photo book layers them), and that is
    itself the signal — a page whose images cover it several times over is not a text page.
    """
    character_count = len(page.get_text().strip())
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return character_count, 0.0
    covered = 0.0
    for image in page.get_images(full=True):
        for rectangle in page.get_image_rects(image[0]):
            covered += abs(rectangle.width * rectangle.height)
    return character_count, covered / page_area * 100.0


def run(input_directories: list[Path], limit: int | None, show_real_names: bool) -> int:
    import pymupdf

    documents = sorted(
        path
        for directory in input_directories
        for path in directory.rglob("*.pdf")
        if path.is_file()
    )
    if limit is not None:
        documents = documents[:limit]
    if not documents:
        print("No PDF found (inputs/ is gitignored — point --inputs at a real corpus).")
        return 2

    print(
        f"text-layer gate: a page goes native at >= {TEXT_LAYER_MINIMUM_CHARACTERS} characters"
    )
    print(
        f"what-if criterion reported below: coverage > {CANDIDATE_COVERAGE_PERCENT:.0f} % "
        f"AND < {CANDIDATE_MAXIMUM_CHARACTERS} characters\n"
    )

    total_pages = blind_pages = ocr_pages = 0
    affected_documents = 0
    all_coverages: list[float] = []
    for index, document_path in enumerate(documents):
        try:
            document = pymupdf.open(document_path)
        except (
            Exception
        ) as error:  # a corpus file we cannot open is a finding, not a crash
            print(
                f"{_label(document_path, index, show_real_names)}: UNREADABLE ({error})"
            )
            continue
        characters: list[int] = []
        coverages: list[float] = []
        with document:
            for page in document:
                character_count, coverage = _page_measurements(page)
                characters.append(character_count)
                coverages.append(coverage)
        if not characters:
            continue
        all_coverages.extend(coverages)
        page_count = len(characters)
        to_ocr_today = sum(
            1 for count in characters if count < TEXT_LAYER_MINIMUM_CHARACTERS
        )
        blind = sum(
            1
            for count, coverage in zip(characters, coverages)
            if count >= TEXT_LAYER_MINIMUM_CHARACTERS
            and coverage > CANDIDATE_COVERAGE_PERCENT
            and count < CANDIDATE_MAXIMUM_CHARACTERS
        )
        total_pages += page_count
        ocr_pages += to_ocr_today
        blind_pages += blind
        if blind:
            affected_documents += 1
        marker = "  <-- read blind" if blind else ""
        print(
            f"{_label(document_path, index, show_real_names):34} "
            f"{page_count:3} p | chars med {statistics.median(characters):6.0f} | "
            f"cover med {statistics.median(coverages):6.1f}% | "
            f"OCR today {to_ocr_today:3} | blind {blind:3}{marker}"
        )

    print(f"\n--- corpus: {len(documents)} PDF, {total_pages} pages ---")
    print(
        f"pages already routed to OCR today (< {TEXT_LAYER_MINIMUM_CHARACTERS} chars): {ocr_pages}"
    )
    print(
        f"pages read BLIND under the what-if criterion: {blind_pages} "
        f"({blind_pages / total_pages * 100:.1f}% of pages, in {affected_documents} document(s))"
    )
    print(
        f"-> wiring it would ADD {blind_pages} page(s) of OCR. Measured cost elsewhere in this "
        "project: RapidOCR 3.7-20.7 s per image."
    )
    if all_coverages:
        ordered = sorted(all_coverages)
        print(
            f"image coverage across all pages (%): min {ordered[0]:.0f} | "
            f"median {statistics.median(ordered):.0f} | "
            f"p90 {ordered[int(len(ordered) * 0.9)]:.0f} | max {ordered[-1]:.0f}"
        )
    print(
        "\nThis pass MEASURES. The threshold, and whether to wire one, is the user's call."
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="*", default=[str(DEFAULT_INPUT_DIRECTORY)])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--names",
        action="store_true",
        help="show real file names (LOCAL use only: a filename can carry a person's name)",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        run([Path(item) for item in arguments.inputs], arguments.limit, arguments.names)
    )
