"""CI anchor-lane fingerprint — the ISO-OUTPUT oracle for changes to the geometry rules.

    uv run python ci_geometry_fingerprint.py                 # default: inputs/recto_verso.pdf
    uv run python ci_geometry_fingerprint.py <recto_verso>   # any CI upload (1+ files)

WHY THIS EXISTS. `ci_fr_electronique_2021_recto` is the ONLY committed template built on geometry
anchors (7 anchor fields, 0 pattern) — every other template extracts by regex. So it is the only
thing exercising `_value_below` / `_value_right` and their tolerances. Any change to coordinates,
units or those rules is a REFACTOR ISO-SORTIE: the extraction must come out IDENTICAL. Unit tests
cannot prove that — only replaying a real document can. Capture before, capture after, `diff`.

    uv run python ci_geometry_fingerprint.py > before.txt
    # ... change the geometry ...
    uv run python ci_geometry_fingerprint.py > after.txt
    diff before.txt after.txt        # empty = the change did not alter extraction

Used this way twice on 2026-07-21: to prove that normalizing `ProvenanceSpan` to page fractions
changed nothing, then that re-expressing the tolerances in LINE HEIGHTS (they were absolute pixels,
so a card scanned at 2200 px instead of ~1100 px silently tightened the rules) left the extraction
byte-identical. Keep using it for anything that touches the geometry.

PII: the input is a REAL identity document. Values are NEVER printed — only a truncated SHA-256,
which compares just as well and leaks nothing. Safe to run and to paste into a commit or an issue.
`inputs/` is gitignored; this harness is not, and holds no data.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from ocr_bifunction.pipeline import process_ci_submission

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"
DEFAULT_DOCUMENT = PROJECT_ROOT / "inputs" / "recto_verso.pdf"
CI_CATEGORY = "carte_identite"


def fingerprint(value: str | None) -> str:
    """A stable, PII-free stand-in for a value: identical input -> identical digest."""
    if value is None:
        return "<None>"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def run(document_paths: list[Path]) -> int:
    missing = [path for path in document_paths if not path.exists()]
    if missing:
        print(f"MISSING: {', '.join(str(path) for path in missing)}")
        print("(inputs/ is gitignored — point the harness at a real CI upload)")
        return 2

    from ocr_bifunction.rapidocr_engine import RapidOcrEngine

    result = process_ci_submission(
        document_paths, RapidOcrEngine(), TEMPLATES_DIRECTORY, category=CI_CATEGORY
    )
    print(f"status = {result.status}")
    print(f"missing = {result.missing}")
    if result.record is None:
        print("record = None")
        print(f"reasons = {result.reasons}")
        return 0

    record = result.record
    print(f"template_id = {record.template_id}")
    print(f"verdict = {record.verdict.value}")
    print(f"mrz_format = {record.mrz_format}")
    print(f"verso_read_path = {record.verso_read_path}")
    print(f"key_matches = {sorted(record.key_matches.items())}")
    print(f"failed_checks = {sorted(record.failed_checks)}")
    print(f"reasons_count = {len(record.reasons)}")
    print("--- fields (name | origin | sha256[:16] of value | span count) ---")
    for name in sorted(record.fields):
        extracted = record.fields[name]
        print(
            f"{name} | {extracted.origin} | {fingerprint(extracted.value)} "
            f"| spans={len(extracted.spans)}"
        )
    return 0


if __name__ == "__main__":
    paths = [Path(argument) for argument in sys.argv[1:]] or [DEFAULT_DOCUMENT]
    raise SystemExit(run(paths))
