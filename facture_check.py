"""Validate born-digital invoices: read -> match a facture template -> config-driven check.

    uv run python facture_check.py <invoice.pdf> [<invoice.pdf> ...]

The structured lane for invoices, the value-check sibling of hp_check.py (a presence check).
Each invoice is read by PyMuPDF's text layer (born-digital, milliseconds, no OCR engine),
matched to a `facture` template, its fields rebuilt, then validated against the template's
`validation` block. The checks are CONFIG-DRIVEN and travel with the template:
  - full-VAT layouts declare a SUM check (montant_ht + montant_tva == montant_ttc);
  - autoliquidation / 293 B franchise layouts have no TVA/TTC line, so they declare only a
    presence check on the HT — the absent sum rule is the template's honest statement.
A document matching no invoice template (e.g. a mise-en-demeure letter) -> human (intruder).

Scope: born-digital PDFs only (the proven corpus). A scanned invoice would need an OCR
engine wired into read_document — out of scope for this lane until one appears.

No PII lives in this file: paths come from the command line (the real inputs are gitignored)
and party/amount values appear only in the runtime output, never in the repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ocr_bifunction.reader import read_document
from ocr_bifunction.template import (
    extract_fields,
    load_templates,
    match_template,
    validate_fields,
)

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"
FACTURE_CATEGORY = "facture"


def validate_facture(
    document_path: Path,
) -> tuple[str, list[str], str | None, dict[str, str | None]]:
    """Return (verdict, reasons, template_id, fields) for one born-digital invoice."""
    result = read_document(document_path)  # born-digital: text layer, no OCR engine
    template = match_template(
        result.lines, load_templates(TEMPLATES_DIRECTORY, FACTURE_CATEGORY)
    )
    if template is None:
        return (
            "human",
            ["not a recognized invoice layout (intruder or unreadable signature)"],
            None,
            {},
        )
    fields = extract_fields(result.lines, template)
    reasons = validate_fields(fields, template.get("validation", {}))
    verdict = "human" if reasons else "auto"
    return verdict, reasons, template["template_id"], fields


def main(document_paths: list[Path]) -> int:
    auto_count = 0
    for document_path in document_paths:
        verdict, reasons, template_id, fields = validate_facture(document_path)
        print("=" * 64)
        print(f"{document_path.name} -> template {template_id}")
        for field_name, field_value in fields.items():
            print(f"  {field_name}: {field_value}")
        print(f"VERDICT: {verdict.upper()}")
        for reason in reasons:
            print("  -", reason)
        auto_count += verdict == "auto"
    print("=" * 64)
    print(f"AUTO {auto_count}/{len(document_paths)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run python facture_check.py <invoice.pdf> [<invoice.pdf> ...]")
        sys.exit(1)
    main([Path(argument) for argument in sys.argv[1:]])
