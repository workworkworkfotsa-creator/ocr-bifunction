"""A/B preprocessors on one image: raw vs rectify vs enhance, with MRZ detection.

    uv run python proofs/preprocess_ab.py "inputs/IMG_8392.jpeg"

Smoke-first: prove an enhancement chain helps a hard case (the angled verso / its
MRZ) BEFORE wiring it as a raw-first -> retry cascade. Each non-raw output image is
dumped to outputs/ for visual inspection. No auto-verdict — compare and decide.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ocr_bifunction.extraction.mrz import extract_mrz_lines, parse_mrz
from ocr_bifunction.reading.preprocess import (
    EnhancePreprocessor,
    NoPreprocessor,
    PerspectiveRectifier,
    Preprocessor,
)
from ocr_bifunction.reading.reader import TextLine

from ocr_bifunction.paths import PROJECT_ROOT

OUTPUT_DIRECTORY = PROJECT_ROOT / "outputs"


def _summarize(label: str, lines: list[TextLine]) -> None:
    text = "\n".join(line.text for line in lines)
    scores = [line.confidence for line in lines if line.confidence is not None]
    confidence = f"{sum(scores) / len(scores):.2f}" if scores else "-"
    print(f"\n--- {label}: {len(lines)} lines, {len(text)} chars, conf {confidence}")
    mrz_lines = extract_mrz_lines(lines)
    if mrz_lines:
        print(f"  MRZ: {len(mrz_lines)} lines -> {parse_mrz(mrz_lines)}")
    else:
        print("  MRZ: none detected")


def run_ab(image_path: Path) -> None:
    from ocr_bifunction.reading.engines.rapidocr_engine import RapidOcrEngine

    OUTPUT_DIRECTORY.mkdir(exist_ok=True)
    raw_bytes = image_path.read_bytes()
    print("Loading RapidOCR engine...")
    engine = RapidOcrEngine()

    preprocessors: list[Preprocessor] = [
        NoPreprocessor(),
        PerspectiveRectifier(),
        EnhancePreprocessor(),
    ]
    for preprocessor in preprocessors:
        processed_bytes = preprocessor.process(raw_bytes)
        if preprocessor.name != "raw":
            (
                OUTPUT_DIRECTORY / f"{image_path.stem}__{preprocessor.name}.png"
            ).write_bytes(processed_bytes)
        _summarize(preprocessor.name, engine.recognize(processed_bytes))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run python proofs/preprocess_ab.py <image_path>")
        sys.exit(1)
    run_ab(Path(sys.argv[1]))
