"""D3 -> D2 promotion runner — prove the organic-growth loop end to end (deterministic, no llama).

    uv run python promotion_check.py <doc> [--held-out facture_entrante_03] [--store PATH]

Proves the whole thesis in ONE store (ocr_jobs=D1, ocr_reviews=D3, ocr_templates=D2): a layout D2 does
NOT know is a miss today; a reviewer curates its template (staged in D3, validated); promotion
activates it in D2; the SAME doc then matches DETERMINISTICALLY and the SLM is not needed again.

  1. seed D2 from templates/ EXCEPT the held-out template (simulate "this layout is unknown");
  2. the doc misses match_template over D2.active_templates() (its template is absent);
  3. a D1 needs_review job + a D3 review stage the curated template as a PENDING suggestion;
  4. the human validates -> promote_suggestion activates it in D2 and flips D3 -> validated;
  5. the doc now MATCHES over D2.active_templates(), and extract+validate gives an auto verdict.

The curated template is the committed (already anonymized) held-out JSON, so nothing PII is written;
D2 lives in a gitignored .sqlite. No field VALUES are printed — only template ids and check names.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ocr_bifunction.promotion import promote_suggestion
from ocr_bifunction.reader import read_document
from ocr_bifunction.repository import STATUS_NEEDS_REVIEW, Job, SqliteRepository
from ocr_bifunction.review_repository import (
    Review,
    Suggestion,
    SqliteReviewRepository,
)
from ocr_bifunction.template import (
    extract_fields,
    field_values,
    match_template,
    validate_fields,
)
from ocr_bifunction.template_repository import SqliteTemplateRepository

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


class _LazyRapidOcrEngine:
    """Build RapidOCR only if an image actually needs OCR (born-digital docs read via text layer)."""

    name = "rapidocr(lazy)"

    def __init__(self) -> None:
        self._engine = None

    def recognize(self, image_png_bytes: bytes):
        if self._engine is None:
            from ocr_bifunction.rapidocr_engine import RapidOcrEngine

            self._engine = RapidOcrEngine()
            self.name = self._engine.name
        return self._engine.recognize(image_png_bytes)


def _seed_d2_except(
    template_repository: SqliteTemplateRepository, held_out: str
) -> int:
    """Seed D2 from templates/ but skip the held-out template — the layout D2 'does not know yet'."""
    seeded = 0
    for path in sorted(TEMPLATES_DIRECTORY.glob("*.json")):
        if path.stem == held_out:
            continue
        template_repository.upsert(
            json.loads(path.read_text(encoding="utf-8")), active=True
        )
        seeded += 1
    return seeded


def run(document_path: Path, held_out: str, store_path: str) -> int:
    Path(store_path).unlink(missing_ok=True)  # fresh run -> deterministic ids
    held_out_file = TEMPLATES_DIRECTORY / f"{held_out}.json"
    if not held_out_file.exists():
        print(f"FAIL: held-out template {held_out_file} not found.")
        return 2
    curated_template = json.loads(held_out_file.read_text(encoding="utf-8"))
    category = curated_template.get("category")

    engine = _LazyRapidOcrEngine()
    result = read_document(document_path, engine)
    print(f"document = {document_path.name}  (held-out layout: {held_out})")

    d1 = SqliteRepository(store_path)
    d2 = SqliteTemplateRepository(store_path)
    d3 = SqliteReviewRepository(store_path)
    try:
        seeded = _seed_d2_except(d2, held_out)
        print(f"D2 seeded with {seeded} template(s) (held out: {held_out})")

        # 2. BEFORE: the doc misses — D2 does not know this layout.
        before = match_template(result.lines, d2.active_templates(category))
        print(f"BEFORE promotion: match = {before['template_id'] if before else None}")
        if before is not None:
            print(
                "FAIL: expected a MISS before promotion (held-out template still matched)."
            )
            return 1

        # 3. A D1 needs_review job + a D3 review staging the curated template (pending).
        job_id = d1.save(
            Job(
                source=document_path.name,
                category_lane="rag",  # unmatched today -> would land in the RAG/review pile
                status=STATUS_NEEDS_REVIEW,
                reasons=["no template matched (layout unknown to D2)"],
            )
        )
        match_anchors = curated_template.get("match", {}).get("all_anchors", [])
        review_id = d3.open_review(
            Review(
                job_id=job_id,
                projection={"source": document_path.name, "lane": "structured"},
                suggestion=Suggestion(
                    template_id=held_out, category=category, anchors=match_anchors
                ),
            )
        )
        print(
            f"staged D3 review #{review_id} (job #{job_id}); pending suggestions: "
            f"{len(d3.pending_suggestions())}"
        )

        # 4. The human validates -> promotion activates the template in D2 + flips D3 validated.
        promoted_id = promote_suggestion(
            review_id,
            curated_template,
            template_repository=d2,
            review_repository=d3,
        )
        review = d3.get(review_id)
        print(
            f"promoted '{promoted_id}' to D2 (active); D3 suggestion status = "
            f"{review.suggestion.status if review and review.suggestion else None}; "
            f"pending now: {len(d3.pending_suggestions())}"
        )

        # 5. AFTER: the same doc now matches deterministically over D2, and extract+validate -> auto.
        after = match_template(result.lines, d2.active_templates(category))
        print(f"AFTER promotion:  match = {after['template_id'] if after else None}")
        if after is None:
            print("FAIL: expected a MATCH after promotion.")
            return 1
        reasons = validate_fields(
            field_values(extract_fields(result.lines, after)),
            after.get("validation", {}),
        )
        verdict = "auto" if not reasons else "review"
        print(f"extract + validate -> {verdict}")
        for reason in reasons:
            print(f"  - {reason}")

        passed = (
            before is None
            and after["template_id"] == held_out
            and review is not None
            and review.suggestion is not None
            and review.suggestion.status == "validated"
        )
        print(
            f"\nEXPECT growth loop (miss -> curate -> validate -> promote -> match): "
            f"{'PASS' if passed else 'FAIL'}"
        )
        return 0 if passed else 1
    finally:
        d1.close()
        d2.close()
        d3.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prove the D3->D2 promotion / organic-growth loop deterministically."
    )
    parser.add_argument(
        "document", type=Path, help="A document whose template will be held out."
    )
    parser.add_argument(
        "--held-out",
        default="facture_entrante_03",
        help="Template id (stem) to hold out of D2, then curate + promote.",
    )
    parser.add_argument(
        "--store",
        default="promotion_check.sqlite",
        help="SQLite store (D1+D2+D3). Fresh each run; gitignored.",
    )
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.document, arguments.held_out, arguments.store))
