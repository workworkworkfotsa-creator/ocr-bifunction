"""Suggestion lane runner — deterministic-first, SLM last resort, anchors re-verified (BRIEF del. 2).

    uv run python proofs/suggestion_check.py <doc> [--category facture]     # match wins -> SLM asleep
    uv run python proofs/suggestion_check.py <doc> --force-slm               # skip the match, prove the SLM
    uv run python proofs/suggestion_check.py <doc> --stage --store PATH      # stage a verified suggestion in D3

For one real document it shows the whole decision the brief specifies:
  match_template hits            -> the SLM is NOT woken (the free, majority path)
  match_template misses          -> wake the SLM: it proposes an id from the closed list + anchors,
                                    the anchors are re-verified on the OCR ->
                                       verified  -> would try that template (and can stage a D3
                                                    pending suggestion for the human)
                                       not       -> human (UNKNOWN, or a hallucinated justification)

--force-slm bypasses the deterministic short-circuit so the SLM path is provable on a doc that would
otherwise match (this corpus's structured docs match deterministically). Needs llama-swap running.

No PII: the path comes from the command line (inputs are gitignored); values appear only at runtime.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ocr_bifunction.reading.reader import read_document
from ocr_bifunction.storage.review_repository import (
    Review,
    Suggestion,
    SqliteReviewRepository,
)
from ocr_bifunction.extraction.suggestion import SuggestionOutcome, suggest_template
from ocr_bifunction.extraction.template import load_templates, match_template

from ocr_bifunction.paths import PROJECT_ROOT

TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


class _LazyRapidOcrEngine:
    """Build RapidOCR only if an image actually needs OCR (born-digital docs read via text layer)."""

    name = "rapidocr(lazy)"

    def __init__(self) -> None:
        self._engine = None

    def recognize(self, image_png_bytes: bytes):
        if self._engine is None:
            from ocr_bifunction.reading.engines.rapidocr_engine import RapidOcrEngine

            self._engine = RapidOcrEngine()
            self.name = self._engine.name
        return self._engine.recognize(image_png_bytes)


def _print_outcome(outcome: SuggestionOutcome) -> None:
    print(f"  suggested_template_id = {outcome.suggested_template_id}")
    print(f"  proposed anchors      = {outcome.proposed_anchors}")
    print(
        f"  confirmed anchors     = {outcome.confirmed_anchors}  (gate 1: re-verified on OCR)"
    )
    if outcome.tried:
        result = "PASS" if not outcome.validation_reasons else "FAIL"
        print(f"  tried template        = {result} (gate 2: extract + validate)")
        for reason in outcome.validation_reasons:
            print(f"      - {reason}")
    print(f"  VERIFIED              = {outcome.verified}")
    if outcome.verified:
        print(
            "  -> the doc FITS: stage a D3 pending suggestion for the human to validate"
        )
    else:
        print(
            "  -> HUMAN (UNKNOWN, hallucinated anchors, or the tried template did not validate)"
        )


def _stage_in_d3(outcome: SuggestionOutcome, source: str, store_path: str) -> None:
    """Stage a verified suggestion as a D3 pending review row (the human then validates)."""
    review_repository = SqliteReviewRepository(store_path)
    try:
        review = Review(
            job_id=0,  # no real D1 job in this runner; the lane -> D3 link is what we show
            projection={"source": source, "lane": "structured"},
            suggestion=Suggestion(
                template_id=outcome.suggested_template_id,
                category=None,
                anchors=outcome.confirmed_anchors,
            ),
        )
        review_id = review_repository.open_review(review)
        pending = review_repository.pending_suggestions()
        print(
            f"\n-- D3 staged: review #{review_id}; "
            f"{len(pending)} pending suggestion(s) in the human's queue --"
        )
    finally:
        review_repository.close()


def run(
    document_path: Path,
    category: str | None,
    force_slm: bool,
    stage: bool,
    store_path: str,
) -> int:
    engine = _LazyRapidOcrEngine()
    result = read_document(document_path, engine)
    print(f"document = {document_path.name}")

    template = match_template(
        result.lines, load_templates(TEMPLATES_DIRECTORY, category)
    )
    if template is not None and not force_slm:
        print(
            f"deterministic MATCH -> {template['template_id']} "
            f"(category {template.get('category')}). SLM NOT woken (free path)."
        )
        return 0

    if template is not None and force_slm:
        print(
            f"(--force-slm: skipping the deterministic match on {template['template_id']} "
            "to exercise the SLM path)"
        )
    else:
        print("no deterministic match -> waking the SLM (last resort)")

    outcome = suggest_template(
        result.text, result.lines, TEMPLATES_DIRECTORY, category=category
    )
    _print_outcome(outcome)

    if stage and outcome.verified:
        _stage_in_d3(outcome, document_path.name, store_path)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prove the deterministic-first template suggestion lane on a real document."
    )
    parser.add_argument("document", type=Path, help="Document path.")
    parser.add_argument(
        "--category",
        default=None,
        help="Scope the closed list to one category (e.g. facture).",
    )
    parser.add_argument(
        "--force-slm",
        action="store_true",
        help="Bypass the deterministic match to exercise the SLM path on a matching doc.",
    )
    parser.add_argument(
        "--stage",
        action="store_true",
        help="Stage a verified suggestion as a D3 pending review row.",
    )
    parser.add_argument(
        "--store",
        default="suggestion_check.sqlite",
        help="SQLite store for --stage (D3 ocr_reviews). Gitignored.",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        run(
            arguments.document,
            arguments.category,
            arguments.force_slm,
            arguments.stage,
            arguments.store,
        )
    )
