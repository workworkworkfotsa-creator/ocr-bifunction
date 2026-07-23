"""2-lane router runner — route a mix of documents and show each one's lane + product.

    uv run python proofs/route_check.py <doc> [<doc> ...]

For each document: STRUCTURED (matched a template -> fields + auto/human verdict) or RAG
(no match -> extractive summary + indexable chunk count). One entry point over what
hp_check.py / facture_check.py / rag_check.py did separately.

The OCR engine is built LAZILY, only when an image/scanned doc actually needs it, so a run
over born-digital PDFs / docx stays fast (no ONNX load). CI pairs are not handled here —
they keep their own pair entry point (process_ci_pair).

No PII lives in this file: paths come from the command line and content appears only in the
runtime output, never in the repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ocr_bifunction.reading.reader import TextLine
from ocr_bifunction.flow.router import RoutedDocument, route_document

from ocr_bifunction.paths import PROJECT_ROOT

TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


class _LazyRapidOcrEngine:
    """OcrEngine that builds the real RapidOCR engine on first use, then delegates.

    Born-digital docs read via their text layer and never call recognize(), so the heavy
    ONNX load is paid only when an actual image/scanned page reaches the engine.
    """

    name = "rapidocr(lazy)"

    def __init__(self) -> None:
        self._engine = None

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        if self._engine is None:
            from ocr_bifunction.reading.engines.rapidocr_engine import RapidOcrEngine

            self._engine = RapidOcrEngine()
            self.name = self._engine.name
        return self._engine.recognize(image_png_bytes)


def _print_routed(routed: RoutedDocument) -> None:
    print("=" * 64)
    print(f"{routed.source}  ->  lane: {routed.lane.upper()}")
    if routed.lane == "structured":
        print(f"  template: {routed.template_id}  (category: {routed.category})")
        for field_name, field_value in routed.fields.items():
            print(f"    {field_name}: {field_value}")
        print(f"  VERDICT: {routed.verdict.value.upper() if routed.verdict else '?'}")
        for reason in routed.reasons:
            print(f"    - {reason}")
    else:  # rag
        if routed.summary is None:
            for reason in routed.reasons:
                print(f"  - {reason}")
            return
        print(f"  {routed.chunk_count} chunk(s) indexable")
        print(f"  keywords: {', '.join(routed.summary.keywords) or '(none)'}")
        for sentence in routed.summary.key_sentences:
            print(f"    • {sentence}")


def main(document_paths: list[Path]) -> int:
    engine = _LazyRapidOcrEngine()
    structured_count = 0
    rag_count = 0
    for document_path in document_paths:
        routed = route_document(document_path, TEMPLATES_DIRECTORY, engine)
        _print_routed(routed)
        structured_count += routed.lane == "structured"
        rag_count += routed.lane == "rag"
    print("=" * 64)
    print(f"STRUCTURED {structured_count}  |  RAG {rag_count}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: uv run python proofs/route_check.py <doc> [<doc> ...]")
        sys.exit(1)
    main([Path(argument) for argument in sys.argv[1:]])
