"""Validate HP 'preuve de test' images: a test ran + an access-id is PRESENT.

    uv run python hp_check.py <image> [<image> ...]

Config-driven and minimal: the image must MATCH the test-page signature (proves a test
ran) and carry the REQUIRED fields declared in the template's `validation` block. The
access-id is a PRESENCE check — its existence proves the machine reached HP's central
system; the exact value is post-processing, not OCR-critical, so a truncated read still
validates. A non-test page (e.g. a BIOS info screen) matches no template -> human
(intruder). Reads RapidOCR raw, no VLM escalation.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ocr_bifunction.reader import OcrEngine, read_document
from ocr_bifunction.template import (
    extract_fields,
    load_templates,
    match_template,
    validate_fields,
)

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"
HP_CATEGORY = "preuve_test"


def validate_preuve_test(
    image_path: Path, engine: OcrEngine
) -> tuple[str, list[str], str | None, dict[str, str | None]]:
    """Return (verdict, reasons, template_id, fields) for one HP test image."""
    result = read_document(image_path, engine)
    template = match_template(
        result.lines, load_templates(TEMPLATES_DIRECTORY, HP_CATEGORY)
    )
    if template is None:
        return (
            "human",
            ["not a recognized HP test page (intruder or unreadable signature)"],
            None,
            {},
        )
    fields = extract_fields(result.lines, template)
    reasons = validate_fields(fields, template.get("validation", {}))
    verdict = "human" if reasons else "auto"
    return verdict, reasons, template["template_id"], fields


def main(image_paths: list[Path]) -> int:
    from ocr_bifunction.rapidocr_engine import RapidOcrEngine

    engine = RapidOcrEngine()
    auto_count = 0
    for image_path in image_paths:
        verdict, reasons, template_id, fields = validate_preuve_test(image_path, engine)
        print("=" * 64)
        print(f"{image_path.name} -> template {template_id}")
        for field_name, field_value in fields.items():
            print(f"  {field_name}: {field_value}")
        print(f"VERDICT: {verdict.upper()}")
        for reason in reasons:
            print("  -", reason)
        auto_count += verdict == "auto"
    print("=" * 64)
    print(f"AUTO {auto_count}/{len(image_paths)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run python hp_check.py <image> [<image> ...]")
        sys.exit(1)
    main([Path(argument) for argument in sys.argv[1:]])
