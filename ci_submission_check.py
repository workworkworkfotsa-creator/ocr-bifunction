"""CI submission check — one upload (any mix of images/PDFs) -> completeness + verdict.

    uv run python ci_submission_check.py <file> [<file> ...]
    uv run python ci_submission_check.py inputs/recto_verso.pdf --escalate

Shows the upload-facing outcome of process_ci_submission, the three scenarios the upload
interface must handle:
  - COMPLETE     : both sides received -> reconciled record + auto/human verdict;
  - INCOMPLETE   : only one side -> names the missing side to ask the user for;
  - UNRECOGNIZED : no CI recto template and no MRZ -> not a CI submission.

A submission can be a single combined PDF/photo (both sides on one page), two separate
photos, or a single side. --escalate wires the heavy VLM tier for an unreadable verso MRZ.

No PII lives in this file: paths come from the command line and identity values appear only
in the runtime output, never in the repo.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ocr_bifunction.pipeline import process_ci_submission
from ocr_bifunction.reader import OcrEngine

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


def main(source_paths: list[Path], escalate: bool) -> int:
    from ocr_bifunction.rapidocr_engine import RapidOcrEngine

    engine = RapidOcrEngine()
    escalation_engine: OcrEngine | None = None
    if escalate:
        from ocr_bifunction.lightonocr_engine import LightOnOcrEngine

        escalation_engine = LightOnOcrEngine()

    result = process_ci_submission(
        source_paths,
        engine,
        TEMPLATES_DIRECTORY,
        escalation_engine=escalation_engine,
    )

    print("=" * 64)
    print(f"upload: {', '.join(path.name for path in source_paths)}")
    print(f"STATUS: {result.status.upper()}")
    if result.missing:
        print(f"  missing side(s): {result.missing}")
    for reason in result.reasons:
        print(f"  - {reason}")

    record = result.record
    if record is not None:
        print(f"  template: {record.template_id}  (mrz: {record.mrz_format})")
        print(f"  verso read: {record.verso_read_path}")
        for field_name, field_value in record.fields.items():
            print(f"    {field_name}: {field_value}")
        if record.key_matches:
            agree = sum(record.key_matches.values())
            print(f"  key matches: {agree}/{len(record.key_matches)} agree")
        print(f"  VERDICT: {record.verdict.value.upper()}")
        for reason in record.reasons:
            print(f"    - {reason}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Check a CI upload (images/PDFs): complete / incomplete / unrecognized."
    )
    parser.add_argument("files", type=Path, nargs="+", help="The uploaded file(s).")
    parser.add_argument(
        "--escalate",
        action="store_true",
        help="Wire the heavy VLM escalation tier for an unreadable verso MRZ.",
    )
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.files, arguments.escalate))
