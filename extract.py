"""Stage ②③ proof — read a document, match a category template, rebuild fields.

    uv run python extract.py "inputs/Carte_identité_électronique_française_(2021,_recto).png"

This is the "pont" ②→③: the OCR lines (with boxes) come from stage ①, a template is
matched by its signature anchors (②), and the named fields are rebuilt from geometry
by deterministic Python (③) — the structured result the Backoffice validates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ocr_bifunction.reader import read_document
from ocr_bifunction.template import (
    extract_fields,
    field_payload,
    load_templates,
    match_template,
)

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


def extract_document(document_path: Path) -> None:
    from ocr_bifunction.rapidocr_engine import RapidOcrEngine

    print("Loading RapidOCR engine...")
    result = read_document(document_path, RapidOcrEngine())

    template = match_template(result.lines, load_templates(TEMPLATES_DIRECTORY))
    if template is None:
        print(
            f"No template matched for {document_path.name} (confidence {result.confidence})."
        )
        return

    fields = extract_fields(result.lines, template)
    print(f"\ndocument : {document_path.name}")
    print(f"template : {template['template_id']}  (category: {template['category']})")
    # None on a native text-layer read (exact, no score to report) — not a missing value.
    confidence = (
        f"{result.confidence:.2f}"
        if result.confidence is not None
        else "n/a (text layer)"
    )
    print(f"ocr confidence : {confidence}\n")
    # The diagnostic CLI shows the FULL shape (value + origin + page/bbox), which is exactly
    # what D1 stores — this is where you eyeball whether a field's provenance landed.
    print(json.dumps(field_payload(fields), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run python extract.py <document_path>")
        sys.exit(1)
    extract_document(Path(sys.argv[1]))
