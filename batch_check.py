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
from ocr_bifunction.repository import (
    STATUS_DONE,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    Job,
    SqliteRepository,
)
from ocr_bifunction.review_repository import (
    Review,
    Suggestion,
    SqliteReviewRepository,
)
from ocr_bifunction.template import load_templates

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
    print(f"  REJECT (proven invalid, terminal):  {len(result.rejected)}")
    if result.review:
        print("\n-- review queue --")
        for record in result.review:
            reason = record.reasons[0] if record.reasons else record.detail
            print(f"  · {record.source}  [{record.lane}/{record.detail}]  <- {reason}")
    if result.rejected:
        print("\n-- rejected (invalid) --")
        for record in result.rejected:
            reason = record.reasons[0] if record.reasons else record.detail
            print(f"  · {record.source}  [{record.lane}/{record.detail}]  <- {reason}")


def _job_from_record(record: DocumentRecord) -> Job:
    """Bridge a batch record to a D1 job row. The RUNNER owns this mapping so orchestrator and
    repository stay independent (neither imports the other). auto -> done/auto; reject ->
    rejected/reject (proven invalid, terminal); anything else doubtful -> needs_review."""
    if record.outcome == "auto":
        status, verdict = STATUS_DONE, "auto"
    elif record.outcome == "reject":
        status, verdict = STATUS_REJECTED, "reject"
    else:
        status = STATUS_NEEDS_REVIEW
        verdict = "human" if record.detail in ("human", "complete") else None
    return Job(
        source=record.source,
        category_lane=record.lane,
        status=status,
        verdict=verdict,
        category=record.category,
        template_id=record.template_id,
        record_fields=record.fields,
        reasons=record.reasons,
    )


def _persist(result: BatchResult, store_path: str) -> None:
    """Write every record into D1, then RE-READ the review queue FROM the store — proving the
    ⑤ queue is a table query ('en attente' = a status), not an in-memory list. A record whose
    suggester outcome VERIFIED also opens a D3 review staging the suggestion `pending` (the
    growth loop fed by the live flow; the human validates -> promotion, cf. promotion.py)."""
    repository = SqliteRepository(store_path)
    review_repository = SqliteReviewRepository(store_path)
    try:
        staged_review_ids: list[int] = []
        for record in result.records:
            job_id = repository.save(_job_from_record(record))
            if record.suggestion is not None and record.suggestion.verified:
                staged_review_ids.append(
                    review_repository.open_review(
                        Review(
                            job_id=job_id,
                            projection={"source": record.source, "lane": record.lane},
                            suggestion=Suggestion(
                                template_id=record.suggestion.suggested_template_id,
                                anchors=record.suggestion.confirmed_anchors,
                            ),
                        )
                    )
                )
        waiting = repository.pending(STATUS_NEEDS_REVIEW)
        print(f"\n-- D1 store: {store_path} --")
        print(
            f"  persisted {len(result.records)} job(s); {len(waiting)} in status "
            f"'{STATUS_NEEDS_REVIEW}' (queried from the table):"
        )
        for job in waiting:
            reason = job.reasons[0] if job.reasons else job.category_lane
            print(f"    #{job.job_id} {job.source}  [{job.category_lane}]  <- {reason}")
        if staged_review_ids:
            pending = review_repository.pending_suggestions()
            print(
                f"  D3: staged {len(staged_review_ids)} verified suggestion(s); "
                f"{len(pending)} pending (queried from ocr_reviews):"
            )
            for review in pending:
                print(
                    f"    review #{review.review_id} (job #{review.job_id}) suggests "
                    f"'{review.suggestion.template_id}'"
                )
    finally:
        repository.close()
        review_repository.close()


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
    parser.add_argument(
        "--store",
        default="ocr_store.sqlite",
        help="SQLite D1 store path (records + status). Holds extracted fields (PII) -> gitignored.",
    )
    parser.add_argument(
        "--suggest",
        action="store_true",
        help="Wake the SLM on a single-doc no-match to propose a template from the closed "
        "list (needs llama-swap); verified suggestions are staged pending in D3.",
    )
    arguments = parser.parse_args()

    engine = _LazyRapidOcrEngine()
    escalation_engine = None
    if arguments.escalate:
        from ocr_bifunction.lightonocr_engine import LightOnOcrEngine

        escalation_engine = LightOnOcrEngine()

    # One template load for the whole batch: the deterministic match and the SLM's closed
    # list read the SAME list (swap in a D2 repository's active_templates() at IT time).
    template_list = load_templates(TEMPLATES_DIRECTORY)
    suggester = None
    if arguments.suggest:
        from ocr_bifunction.suggestion import SuggestionOutcome, suggest_template

        def suggester(
            text: str, lines: list[TextLine], category: str | None
        ) -> SuggestionOutcome:
            return suggest_template(
                text,
                lines,
                TEMPLATES_DIRECTORY,
                category=category,
                templates=template_list,
            )

    items = _build_items(arguments.documents, arguments.ci, arguments.document_type)
    result = process_batch(
        items,
        TEMPLATES_DIRECTORY,
        engine,
        escalation_engine,
        templates=template_list,
        suggester=suggester,
    )
    for record in result.records:
        _print_record(record)
    _print_split(result)
    _persist(result, arguments.store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
