"""Reconcile a COMBINED recto+verso PDF — one page carrying both card images.

    uv run python reconcile_pdf_check.py inputs/recto_verso.pdf
    uv run python reconcile_pdf_check.py inputs/recto_verso.pdf --escalate

A disposable input adapter: process_ci_pair wants two image paths, but a scan can put both
sides on a single (image-only) page. This extracts the embedded images, then runs the real
pipeline. Which image is the recto is discovered, not assumed: each ordering is tried and the
one whose RECTO matches a CI template wins (the verso side matches no recto template). With
--escalate the heavy VLM tier is wired in for an unreadable verso MRZ.

No PII lives in this file: the path comes from the command line and identity values appear
only in the runtime output, never in the repo.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import pymupdf

from ocr_bifunction.pipeline import CiRecord, process_ci_pair
from ocr_bifunction.reader import OcrEngine

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


def extract_images(pdf_path: Path) -> list[tuple[bytes, str]]:
    """Return every embedded image as (bytes, extension), in page/placement order."""
    images: list[tuple[bytes, str]] = []
    with pymupdf.open(pdf_path) as document:
        for page in document:
            for image_info in page.get_images(full=True):
                extracted = document.extract_image(image_info[0])
                images.append((extracted["image"], extracted["ext"]))
    return images


def reconcile_combined_pdf(
    pdf_path: Path, engine: OcrEngine, escalation_engine: OcrEngine | None
) -> CiRecord | None:
    """Extract the two card images and reconcile them, discovering recto vs verso order."""
    images = extract_images(pdf_path)
    if len(images) < 2:
        print(f"{pdf_path.name}: expected 2 embedded images, found {len(images)}")
        return None
    if len(images) > 2:
        print(f"note: {len(images)} images found, using the first two")
    images = images[:2]

    with tempfile.TemporaryDirectory(prefix="ocr_bifunction_rv_") as temp_directory:
        temp_path = Path(temp_directory)
        paths = []
        for index, (image_bytes, extension) in enumerate(images):
            image_path = temp_path / f"image_{index}.{extension}"
            image_path.write_bytes(image_bytes)
            paths.append(image_path)

        # Discover orientation: the recto is the side that matches a CI template.
        record: CiRecord | None = None
        for recto_index, verso_index in ((0, 1), (1, 0)):
            candidate = process_ci_pair(
                paths[recto_index],
                paths[verso_index],
                engine,
                TEMPLATES_DIRECTORY,
                escalation_engine=escalation_engine,
            )
            record = candidate
            if candidate.template_id is not None:
                print(
                    f"recto detected = image_{recto_index}, verso = image_{verso_index}"
                )
                break
        return record


def main(pdf_path: Path, escalate: bool) -> int:
    from ocr_bifunction.rapidocr_engine import RapidOcrEngine

    engine = RapidOcrEngine()
    escalation_engine: OcrEngine | None = None
    if escalate:
        from ocr_bifunction.lightonocr_engine import LightOnOcrEngine

        escalation_engine = LightOnOcrEngine()

    record = reconcile_combined_pdf(pdf_path, engine, escalation_engine)
    if record is None:
        return 1

    print("=" * 64)
    print(f"{pdf_path.name}")
    print(f"  template   : {record.template_id}  (mrz: {record.mrz_format})")
    print(f"  verso read : {record.verso_read_path}")
    print("  fields:")
    for field_name, field_value in record.fields.items():
        print(f"    {field_name}: {field_value}")
    if record.key_matches:
        print("  key matches (recto vs MRZ):")
        for key, matches in record.key_matches.items():
            print(f"    {key}: {'OK' if matches else 'MISMATCH'}")
    if record.failed_checks:
        print(f"  failed MRZ checks: {record.failed_checks}")
    print(f"  VERDICT: {record.verdict.upper()}")
    for reason in record.reasons:
        print(f"    - {reason}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Reconcile a combined recto+verso CI PDF (both images on one page)."
    )
    parser.add_argument("pdf", type=Path, help="Path to the combined recto+verso PDF.")
    parser.add_argument(
        "--escalate",
        action="store_true",
        help="Wire the heavy VLM escalation tier for an unreadable verso MRZ.",
    )
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.pdf, arguments.escalate))
