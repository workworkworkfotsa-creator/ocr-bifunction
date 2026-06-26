"""A/B a Preprocessor on one image: raw OCR vs enhanced OCR.

    uv run python preprocess_ab.py "inputs/Carte_identité_électronique_française_(2021,_verso).png"

Smoke-first: prove the enhancement chain recovers a hard case (CI verso / MRZ)
BEFORE wiring it into the pipeline as a raw-first -> retry cascade. The enhanced
image is dumped to outputs/ for visual inspection. No auto-verdict — compare and
let the user decide.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ocr_bifunction.preprocess import EnhancePreprocessor, NoPreprocessor, Preprocessor
from ocr_bifunction.reader import TextLine

PROJECT_ROOT = Path(__file__).parent
OUTPUT_DIRECTORY = PROJECT_ROOT / "outputs"


def _summarize(label: str, lines: list[TextLine]) -> None:
    text = "\n".join(line.text for line in lines)
    scores = [line.confidence for line in lines if line.confidence is not None]
    confidence = f"{sum(scores) / len(scores):.2f}" if scores else "-"
    print(f"\n--- {label}: {len(lines)} lines, {len(text)} chars, conf {confidence}")
    print(text)


def run_ab(image_path: Path) -> None:
    from ocr_bifunction.rapidocr_engine import RapidOcrEngine

    OUTPUT_DIRECTORY.mkdir(exist_ok=True)
    raw_bytes = image_path.read_bytes()
    print("Loading RapidOCR engine...")
    engine = RapidOcrEngine()

    preprocessors: list[Preprocessor] = [NoPreprocessor(), EnhancePreprocessor()]
    for preprocessor in preprocessors:
        processed_bytes = preprocessor.process(raw_bytes)
        if preprocessor.name != "raw":
            (
                OUTPUT_DIRECTORY / f"{image_path.stem}__{preprocessor.name}.png"
            ).write_bytes(processed_bytes)
        _summarize(preprocessor.name, engine.recognize(processed_bytes))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run python preprocess_ab.py <image_path>")
        sys.exit(1)
    run_ab(Path(sys.argv[1]))
