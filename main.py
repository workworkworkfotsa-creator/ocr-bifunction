"""OCR BiFunction — single entry point: one CI recto+verso pair -> record + verdict.

    uv run python main.py <recto_image> <verso_image>

The wired pipeline (raw-first verso read, enhance-retry on the hard case), replacing
the piecemeal demo scripts. Heavy: constructing RapidOcrEngine loads the ONNX models
(CPU). On a real concordant pair this prints AUTO with all keys matched; cross a
mismatched pair (recto of A + verso of B) to see it route to HUMAN with reasons.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ocr_bifunction.pipeline import process_ci_pair

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


def main(recto_path: Path, verso_path: Path) -> None:
    from ocr_bifunction.rapidocr_engine import RapidOcrEngine

    record = process_ci_pair(
        recto_path, verso_path, RapidOcrEngine(), TEMPLATES_DIRECTORY
    )

    print(f"recto {recto_path.name} -> template {record.template_id}")
    print(
        f"verso {verso_path.name} -> MRZ {record.mrz_format} "
        f"(read: {record.verso_read_path})"
    )
    print("\nrecord:")
    for key, value in record.fields.items():
        print(f"  {key}: {value}")
    print(f"\nkey_matches: {record.key_matches}")
    print(f"VERDICT: {record.verdict.value.upper()}")
    for reason in record.reasons:
        print("  -", reason)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: uv run python main.py <recto_image> <verso_image>")
        sys.exit(1)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
