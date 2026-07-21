"""Reconcile a recto (template) against a verso MRZ — end-to-end on real cards.

    uv run python reconcile_check.py <recto_image> <verso_image>

The verso is enhanced before OCR (recovers the angle-degraded MRZ line). Cross a
mismatched pair (e.g. a recto with someone else's MRZ) to see the "recto A + verso
B" detection fire.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ocr_bifunction.mrz import MrzFields, extract_mrz_lines, parse_mrz
from ocr_bifunction.preprocess import EnhancePreprocessor
from ocr_bifunction.reader import OcrEngine, read_document
from ocr_bifunction.reconcile import reconcile
from ocr_bifunction.template import (
    extract_fields,
    field_values,
    load_templates,
    match_template,
)

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


def _recto_fields(
    recto_path: Path, engine: OcrEngine
) -> tuple[str | None, dict | None]:
    result = read_document(recto_path, engine)
    template = match_template(result.lines, load_templates(TEMPLATES_DIRECTORY))
    if template is None:
        return None, None
    return template["template_id"], field_values(extract_fields(result.lines, template))


def _verso_mrz(verso_path: Path, engine: OcrEngine) -> MrzFields | None:
    enhanced = EnhancePreprocessor().process(verso_path.read_bytes())
    mrz_lines = extract_mrz_lines(engine.recognize(enhanced))
    return parse_mrz(mrz_lines) if mrz_lines else None


def main(recto_path: Path, verso_path: Path) -> None:
    from ocr_bifunction.rapidocr_engine import RapidOcrEngine

    engine = RapidOcrEngine()
    template_id, recto_fields = _recto_fields(recto_path, engine)
    mrz = _verso_mrz(verso_path, engine)

    print(f"recto {recto_path.name} -> template {template_id}")
    print(f"  {recto_fields}")
    print(f"verso {verso_path.name} -> MRZ {mrz.mrz_format if mrz else None}")
    print(f"  {mrz}")

    if recto_fields is None or mrz is None:
        print("\nVERDICT: HUMAN (recto template or verso MRZ missing)")
        return

    result = reconcile(recto_fields, mrz)
    print(f"\nkey_matches: {result.key_matches}")
    print(f"VERDICT: {result.verdict.value.upper()}")
    for reason in result.reasons:
        print("  -", reason)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: uv run python reconcile_check.py <recto_image> <verso_image>")
        sys.exit(1)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
