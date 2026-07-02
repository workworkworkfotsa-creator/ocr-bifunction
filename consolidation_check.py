"""End-to-end consolidation runner — the whole growth chain in ONE demo, ONE store (no llama).

    uv run python consolidation_check.py <held_out_doc> [<other docs> ...]
        [--held-out facture_entrante_03] [--store PATH]

Proves the bricks assemble WITHOUT holes across the three domains in one SQLite file
(ocr_jobs=D1, ocr_reviews=D3, ocr_templates=D2), driving the REAL batch backbone end to end:

  PHASE A  intake     process_batch over real docs, templates read FROM D2 (the worker's read
                      path per the contrat de colonnes — the seam this runner exercises). D2 is
                      seeded WITHOUT the held-out layout, so the first doc MISSES and lands in
                      review; the other docs behave as usual (a known facture -> auto, a
                      courrier -> RAG/review). Every record persists into D1.
  PHASE B  review     the D1 needs_review queue -> D3 reviews opened (projection, job_id FK).
  PHASE C  curation   the reviewer curates the missing template — the committed (anonymized)
                      JSON stands in for their work — staged as a PENDING D3 suggestion. (The
                      SLM leg of this phase is proven in suggestion_check.py; for a layout truly
                      unknown to D2 its best honest closed-list answer is UNKNOWN, which lands
                      exactly here: a human curates.)
  PHASE D  promotion  validate -> promote_suggestion: D2 gains the ACTIVE template, D3 flips
                      validated, the queue empties.
  PHASE E  re-match   the SAME doc re-runs through route_document reading D2 -> STRUCTURED/auto,
                      and the worker writes the D1 job done/auto — the loop visibly closes in
                      the jobs table itself.

Final state is printed FROM the tables. No PII in this file (paths come from the CLI; only ids,
statuses and field counts are printed). The .sqlite is gitignored.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ocr_bifunction.orchestrator import BatchItem, DocumentRecord, process_batch
from ocr_bifunction.promotion import promote_suggestion
from ocr_bifunction.repository import (
    STATUS_DONE,
    STATUS_NEEDS_REVIEW,
    Job,
    SqliteRepository,
)
from ocr_bifunction.review_repository import (
    DECISION_ACCEPT,
    Review,
    Suggestion,
    SqliteReviewRepository,
)
from ocr_bifunction.router import route_document
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


def _job_from_record(record: DocumentRecord) -> Job:
    """Bridge a batch record to a D1 job row (runner-owned mapping, same as batch_check.py)."""
    if record.outcome == "auto":
        status, verdict = STATUS_DONE, "auto"
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


def _seed_d2_except(
    template_repository: SqliteTemplateRepository, held_out: str
) -> int:
    """Seed D2 from templates/ but skip the held-out one — the layout D2 'does not know yet'."""
    seeded = 0
    for path in sorted(TEMPLATES_DIRECTORY.glob("*.json")):
        if path.stem == held_out:
            continue
        template_repository.upsert(
            json.loads(path.read_text(encoding="utf-8")), active=True
        )
        seeded += 1
    return seeded


def run(document_paths: list[Path], held_out: str, store_path: str) -> int:
    Path(store_path).unlink(missing_ok=True)  # fresh run -> deterministic ids
    held_out_file = TEMPLATES_DIRECTORY / f"{held_out}.json"
    if not held_out_file.exists():
        print(f"FAIL: held-out template {held_out_file} not found.")
        return 2
    curated_template = json.loads(held_out_file.read_text(encoding="utf-8"))
    held_out_source = document_paths[0].name

    engine = _LazyRapidOcrEngine()
    d1 = SqliteRepository(store_path)
    d2 = SqliteTemplateRepository(store_path)
    d3 = SqliteReviewRepository(store_path)
    try:
        seeded = _seed_d2_except(d2, held_out)
        print(f"D2 seeded: {seeded} active template(s), held out: {held_out}\n")

        # PHASE A — intake through the REAL batch backbone, templates FROM D2.
        print("== PHASE A: intake (process_batch, templates from D2) ==")
        items = [BatchItem(paths=[path]) for path in document_paths]
        result = process_batch(
            items, TEMPLATES_DIRECTORY, engine, templates=d2.active_templates()
        )
        job_ids: dict[str, int] = {}
        for record in result.records:
            job_ids[record.source] = d1.save(_job_from_record(record))
            print(
                f"  {record.source} -> {record.lane}/{record.outcome}"
                f" (template: {record.template_id})  D1 job #{job_ids[record.source]}"
            )
        held_out_record = next(
            record for record in result.records if record.source == held_out_source
        )
        if (
            held_out_record.outcome != "review"
            or held_out_record.template_id is not None
        ):
            print("\nFAIL: expected the held-out doc to MISS (review, no template).")
            return 1

        # PHASE B — the D1 review queue -> D3 reviews.
        print("\n== PHASE B: D1 needs_review queue -> D3 reviews ==")
        review_ids: dict[int, int] = {}
        for job in d1.pending(STATUS_NEEDS_REVIEW):
            review_ids[job.job_id] = d3.open_review(
                Review(
                    job_id=job.job_id,
                    projection={
                        "source": job.source,
                        "lane": job.category_lane,
                        "verdict": job.verdict,
                    },
                )
            )
            print(f"  review #{review_ids[job.job_id]} opened for job #{job.job_id}")

        # PHASE C — the reviewer curates the missing template -> staged PENDING on the SAME
        # review row (stage_suggestion: the review was opened at intake, the candidate arrives
        # later — the seam this consolidation surfaced and added to the D3 contract).
        print("\n== PHASE C: curation -> pending suggestion ==")
        held_out_job_id = job_ids[held_out_source]
        held_out_review_id = review_ids[held_out_job_id]
        d3.record_decision(held_out_review_id, decision=DECISION_ACCEPT)
        d3.stage_suggestion(
            held_out_review_id,
            Suggestion(
                template_id=held_out,
                category=curated_template.get("category"),
                anchors=curated_template.get("match", {}).get("all_anchors", []),
            ),
        )
        pending = d3.pending_suggestions()
        print(
            f"  review #{held_out_review_id} stages '{held_out}' -> pending "
            f"suggestions: {len(pending)}"
        )

        # PHASE D — validate -> promote: D2 gains the active template, D3 flips validated.
        print("\n== PHASE D: promotion D3 -> D2 ==")
        promoted_id = promote_suggestion(
            held_out_review_id,
            curated_template,
            template_repository=d2,
            review_repository=d3,
        )
        print(
            f"  promoted '{promoted_id}' (D2 active); pending suggestions now: "
            f"{len(d3.pending_suggestions())}"
        )

        # PHASE E — the SAME doc re-runs against D2 and the worker closes the D1 job.
        print("\n== PHASE E: re-match against D2 + close the D1 job ==")
        routed = route_document(
            document_paths[0],
            TEMPLATES_DIRECTORY,
            engine,
            templates=d2.active_templates(),
        )
        print(
            f"  re-match: {routed.lane}/{routed.verdict} "
            f"(template: {routed.template_id})"
        )
        if routed.template_id != held_out or routed.verdict != "auto":
            print(
                "FAIL: expected a deterministic STRUCTURED/auto match after promotion."
            )
            return 1
        d1.update_status(
            held_out_job_id,
            STATUS_DONE,
            verdict="auto",
            record_fields=routed.fields,
            reasons=[],
        )

        # Final state, read back FROM the tables (the store is the proof, not this script).
        print("\n== FINAL STATE (read from the tables) ==")
        closed_job = d1.get(held_out_job_id)
        print(
            f"  D1 job #{closed_job.job_id}: {closed_job.status}/{closed_job.verdict} "
            f"({len(closed_job.record_fields)} record field(s)) — was needs_review"
        )
        validated_review = d3.get(held_out_review_id)
        print(
            f"  D3 review #{validated_review.review_id}: suggestion "
            f"'{validated_review.suggestion.template_id}' = "
            f"{validated_review.suggestion.status}, decision = {validated_review.decision}"
        )
        active_row = d2.get(promoted_id)
        print(f"  D2 template '{active_row['template_id']}': present, active")

        passed = (
            closed_job.status == STATUS_DONE
            and closed_job.verdict == "auto"
            and validated_review.suggestion.status == "validated"
            and active_row is not None
        )
        print(
            f"\nEXPECT full chain (miss -> D1 -> D3 -> promote -> D2 -> re-match -> "
            f"D1 closed): {'PASS' if passed else 'FAIL'}"
        )
        return 0 if passed else 1
    finally:
        d1.close()
        d2.close()
        d3.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prove the whole growth chain (D1 -> D3 -> D2 -> re-match) in one demo."
    )
    parser.add_argument(
        "documents",
        type=Path,
        nargs="+",
        help="Docs; the FIRST one's template is held out of D2 (it must miss, then re-match).",
    )
    parser.add_argument(
        "--held-out",
        default="facture_entrante_03",
        help="Template id (stem) to hold out of D2, then curate + promote.",
    )
    parser.add_argument(
        "--store",
        default="consolidation_check.sqlite",
        help="SQLite store (D1+D2+D3). Fresh each run; gitignored.",
    )
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.documents, arguments.held_out, arguments.store))
