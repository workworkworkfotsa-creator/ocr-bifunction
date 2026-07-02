"""Batch orchestrator runner — the end-to-end BATCH backbone on a lot of real documents.

    uv run python batch_check.py <doc> [<doc> ...]           # each file = one single doc
    uv run python batch_check.py --ci recto.png verso.png    # group files as ONE CI upload
    uv run python batch_check.py --escalate <doc> ...        # allow LightOCR (needs llama-swap)

For each item the router/pipeline decides the lane and verdict; the run ends on the ④/⑤
split — the AUTO pile (centralise-ready) and the REVIEW queue (what a human must look at).
Persistence is out of scope (see orchestrator.py): this runner PRINTS the two piles.

The OCR engine is built LAZILY (ONNX loads only when an image actually needs it, so a
born-digital batch stays fast). `--escalate` wires LightOCR for the CI verso — it POSTs to
the shared llama-swap, so llama-swap must be running (tools/llama-swap).

No PII lives in this file: paths come from the command line; content appears only at runtime.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ocr_bifunction.orchestrator import (
    CI_CATEGORY,
    BatchItem,
    BatchResult,
    DocumentRecord,
    process_batch,
)
from ocr_bifunction.reader import TextLine

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


class _LazyRapidOcrEngine:
    """OcrEngine that builds the real RapidOCR engine on first use, then delegates — so a
    born-digital batch never pays the ONNX load (same pattern as route_check.py)."""

    name = "rapidocr(lazy)"

    def __init__(self) -> None:
        self._engine = None

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        if self._engine is None:
            from ocr_bifunction.rapidocr_engine import RapidOcrEngine

            self._engine = RapidOcrEngine()
            self.name = self._engine.name
        return self._engine.recognize(image_png_bytes)


def _build_items(
    paths: list[Path], as_ci: bool, document_type: str | None
) -> list[BatchItem]:
    if as_ci:
        return [BatchItem(paths=paths, document_type=CI_CATEGORY)]
    return [BatchItem(paths=[path], document_type=document_type) for path in paths]


def _print_record(record: DocumentRecord) -> None:
    print("=" * 64)
    print(f"{record.source}  ->  lane: {record.lane.upper()}  [{record.detail}]")
    if record.lane in ("structured", "ci") and record.fields:
        if record.template_id:
            print(f"  template: {record.template_id}  (category: {record.category})")
        for field_name, field_value in record.fields.items():
            print(f"    {field_name}: {field_value}")
    if record.lane == "rag" and record.summary is not None:
        print(f"  {record.chunk_count} chunk(s) indexable")
        print(f"  keywords: {', '.join(record.summary.keywords) or '(none)'}")
    print(f"  OUTCOME: {record.outcome.upper()}")
    for reason in record.reasons:
        print(f"    - {reason}")


def _print_split(result: BatchResult) -> None:
    print("\n" + "#" * 64)
    print(f"BATCH: {len(result.records)} document(s)")
    print(f"  AUTO   (stage 4, centralise-ready): {len(result.auto)}")
    print(f"  REVIEW (stage 5, human queue):      {len(result.review)}")
    if result.review:
        print("\n-- review queue --")
        for record in result.review:
            reason = record.reasons[0] if record.reasons else record.detail
            print(f"  · {record.source}  [{record.lane}/{record.detail}]  <- {reason}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a batch of documents end-to-end; print the ④ auto / ⑤ review split."
    )
    parser.add_argument("documents", type=Path, nargs="+", help="Document paths.")
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Treat ALL given files as ONE CI submission (recto+verso) instead of single docs.",
    )
    parser.add_argument(
        "--document-type",
        default=None,
        help="Declared category for single docs (scopes template matching), e.g. facture.",
    )
    parser.add_argument(
        "--escalate",
        action="store_true",
        help="Allow LightOCR escalation for the CI verso (needs llama-swap running).",
    )
    arguments = parser.parse_args()

    engine = _LazyRapidOcrEngine()
    escalation_engine = None
    if arguments.escalate:
        from ocr_bifunction.lightonocr_engine import LightOnOcrEngine

        escalation_engine = LightOnOcrEngine()

    items = _build_items(arguments.documents, arguments.ci, arguments.document_type)
    result = process_batch(items, TEMPLATES_DIRECTORY, engine, escalation_engine)
    for record in result.records:
        _print_record(record)
    _print_split(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
