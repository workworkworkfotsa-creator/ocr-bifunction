"""Smoke runner for stage ① LIRE — point it at a folder of real documents.

Born-digital PDFs and .docx are read via their native text layer; images and
image-only PDF pages go through the OCR engine. Per document it writes the flat
text (.txt) and the geometry (.json: lines with bbox + score) to outputs/, so the
boxes that stage ③ will anchor on are inspectable.

    uv run python smoke.py            # reads inputs/, OCR on
    uv run python smoke.py some/dir   # reads some/dir
    uv run python smoke.py --no-ocr   # text layer only, skip OCR
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ocr_bifunction.reader import OcrEngine, ReadResult, read_document

PROJECT_ROOT = Path(__file__).parent
DEFAULT_INPUT_DIRECTORY = PROJECT_ROOT / "inputs"
OUTPUT_DIRECTORY = PROJECT_ROOT / "outputs"


def run_smoke(input_directory: Path, ocr_engine: OcrEngine | None = None) -> None:
    OUTPUT_DIRECTORY.mkdir(exist_ok=True)
    documents = sorted(path for path in input_directory.iterdir() if path.is_file())
    print(f"Reading {len(documents)} documents from {input_directory}\n")
    header = f"{'document':<48} {'backend':<22} {'pages':>5} {'lines':>5} {'chars':>7} {'conf':>6} {'sec':>6}"
    print(header)
    print("-" * len(header))

    for document_path in documents:
        result = read_document(document_path, ocr_engine)
        name = document_path.name[:47]
        if result.error:
            print(f"{name:<48} ERROR: {result.error}")
            continue
        confidence_text = (
            "-" if result.confidence is None else f"{result.confidence:.2f}"
        )
        print(
            f"{name:<48} {result.backend_name:<22} {result.page_count:>5} "
            f"{len(result.lines):>5} {result.character_count:>7} {confidence_text:>6} "
            f"{result.elapsed_seconds:>6.2f}"
        )
        _dump_outputs(document_path, result)


def _dump_outputs(document_path: Path, result: ReadResult) -> None:
    stem = document_path.stem
    if result.text:
        (OUTPUT_DIRECTORY / f"{stem}.txt").write_text(result.text, encoding="utf-8")
    if not result.lines:
        return
    payload = {
        "document": document_path.name,
        "backend": result.backend_name,
        "confidence": result.confidence,
        "lines": [
            {
                "text": line.text,
                "bbox": [round(value, 1) for value in line.bbox],
                "confidence": round(line.confidence, 3)
                if line.confidence is not None
                else None,
                "page": line.page_index,
            }
            for line in result.lines
        ],
    }
    (OUTPUT_DIRECTORY / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    arguments = sys.argv[1:]
    use_ocr = "--no-ocr" not in arguments
    positional = [argument for argument in arguments if not argument.startswith("--")]
    target = Path(positional[0]) if positional else DEFAULT_INPUT_DIRECTORY

    engine: OcrEngine | None = None
    if use_ocr:
        from ocr_bifunction.rapidocr_engine import RapidOcrEngine

        print("Loading RapidOCR engine (first run downloads ONNX models)...")
        engine = RapidOcrEngine()
    run_smoke(target, engine)
