"""Parse the French legacy MRZ from a CI image and validate its check digits.

    uv run python mrz_check.py "inputs/French_Identity_card_1988_-_1994.jpg" [more images...]

A failed check digit is a real "→ human" signal (unlike the OCR per-line score).
If no MRZ is detected, all OCR lines are printed to see what the engine produced.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ocr_bifunction.mrz import extract_mrz_lines, icao_check_digit, parse_mrz
from ocr_bifunction.reader import read_document


def check_mrz(image_path: Path) -> None:
    from ocr_bifunction.rapidocr_engine import RapidOcrEngine

    result = read_document(image_path, RapidOcrEngine())
    print(f"\n=== {image_path.name} (ocr conf {result.confidence}) ===")
    mrz_lines = extract_mrz_lines(result.lines)
    if mrz_lines is None:
        print("No MRZ lines detected. OCR lines:")
        for line in result.lines:
            print("  ", repr(line.text))
        return
    print(f"MRZ lines ({len(mrz_lines)}):")
    for line in mrz_lines:
        print("  ", repr(line))
    print("parsed:", parse_mrz(mrz_lines))


if __name__ == "__main__":
    print(
        "sanity: icao_check_digit('870314') =", icao_check_digit("870314"), "(expect 5)"
    )
    for argument in sys.argv[1:]:
        check_mrz(Path(argument))
