"""D3 review store runner — prove the review + suggestion loop end to end (no SLM, no llama).

    uv run python review_check.py [--store PATH]

Mechanically drives the organic-growth loop on synthetic (PII-free) D1 jobs, in ONE store file
(the "one MariaDB, prefixed tables" target — ocr_jobs is D1, ocr_reviews is D3):
  1. seed a couple of `needs_review` jobs in D1 (what the worker leaves behind);
  2. D3 opens a review per job, its `projection` a VIEW of the D1 record (not a copy — the record's
     single source of truth stays in D1, referenced by job_id);
  3. a STUB suggestion (a candidate template id from the closed list + the anchors that motivate it)
     is staged `pending` for the structured-no-template job — this is exactly what the SLM lane will
     produce later;
  4. `pending_suggestions()` IS the human's queue — the status-driven signal, like D1's status;
  5. the human validates the suggestion (-> promote to D2, step 3) and rejects the other record.

No PII: every value is synthetic and generic. The .sqlite holds no real data and is gitignored.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ocr_bifunction.repository import STATUS_NEEDS_REVIEW, Job, SqliteRepository
from ocr_bifunction.review_repository import (
    DECISION_ACCEPT,
    DECISION_REJECT,
    SUGGESTION_VALIDATED,
    Review,
    Suggestion,
    SqliteReviewRepository,
)


def _seed_needs_review_jobs(store_path: str) -> None:
    """Write two synthetic `needs_review` jobs into D1 — the worker's leftovers a human must see:
    a structured doc that matched no template (a suggestion is warranted) and a RAG-lane doc."""
    repository = SqliteRepository(store_path)
    try:
        repository.save(
            Job(
                source="doc_structured_unmatched.pdf",
                category_lane="structured",
                status=STATUS_NEEDS_REVIEW,
                verdict="human",
                category="facture",
                record_fields={"total_ht": "100.00"},
                reasons=["no template matched the signature"],
            )
        )
        repository.save(
            Job(
                source="memo_unstructured.docx",
                category_lane="rag",
                status=STATUS_NEEDS_REVIEW,
                reasons=["non-structured document -> retrieval / human review"],
            )
        )
    finally:
        repository.close()


def _projection(job: Job) -> dict[str, str | None]:
    """The human-facing VIEW of a D1 job (not a second source of truth — the record lives in D1)."""
    return {"source": job.source, "lane": job.category_lane, "verdict": job.verdict}


def _print_reviews(
    review_repository: SqliteReviewRepository, job_ids: list[int]
) -> None:
    print("\n-- D3 ocr_reviews (final) --")
    for job_id in job_ids:
        review = review_repository.by_job(job_id)
        if review is None:
            continue
        suggestion = review.suggestion
        suggestion_text = (
            f"{suggestion.template_id} [{suggestion.status}] anchors={suggestion.anchors}"
            if suggestion
            else "(none)"
        )
        print(
            f"  review #{review.review_id}  job={review.job_id}  "
            f"decision={review.decision}  suggestion={suggestion_text}"
        )
        print(f"     projection: {review.projection}")
        if review.comment:
            print(f"     comment: {review.comment}")


def run(store_path: str) -> int:
    Path(store_path).unlink(missing_ok=True)  # fresh run -> deterministic ids
    _seed_needs_review_jobs(store_path)

    d1 = SqliteRepository(store_path)
    d3 = SqliteReviewRepository(store_path)
    try:
        # D3 reads D1's needs_review queue and opens one review per job. Only the structured doc
        # that matched no template gets a suggestion staged (what the SLM lane will produce).
        job_ids: list[int] = []
        for job in d1.pending(STATUS_NEEDS_REVIEW):
            job_ids.append(job.job_id)
            review = Review(job_id=job.job_id, projection=_projection(job))
            if job.category_lane == "structured":
                review.suggestion = Suggestion(
                    template_id="facture_entrante_01",  # a known id (closed list), generic
                    category="facture",
                    anchors=[
                        "FACTURE",
                        "Total HT",
                    ],  # structural, re-verifiable, no PII
                )
            review_id = d3.open_review(review)
            print(
                f"opened review #{review_id} for job #{job.job_id} [{job.category_lane}]"
            )

        # The status-driven queue: suggestions waiting for the human.
        pending = d3.pending_suggestions()
        print(f"\npending suggestions (the human's queue): {len(pending)}")
        for review in pending:
            print(
                f"  review #{review.review_id}: suggests "
                f"{review.suggestion.template_id} <- anchors {review.suggestion.anchors}"
            )

        # The human validates the staged suggestion (-> promote to D2, step 3) and records accept.
        for review in pending:
            d3.set_suggestion_status(review.review_id, SUGGESTION_VALIDATED)
            d3.record_decision(
                review.review_id,
                comment="anchors confirmed on OCR",
                decision=DECISION_ACCEPT,
            )

        # The RAG-lane review has no suggestion; the human rejects the record (ask for a rescan).
        for job in d1.pending(STATUS_NEEDS_REVIEW):
            if job.category_lane == "rag":
                rag_review = d3.by_job(job.job_id)
                d3.record_decision(
                    rag_review.review_id,
                    comment="unreadable content — request a cleaner document",
                    decision=DECISION_REJECT,
                )

        remaining = d3.pending_suggestions()
        print(f"\npending suggestions after validation: {len(remaining)} (loop closed)")
        _print_reviews(d3, job_ids)

        passed = len(pending) == 1 and len(remaining) == 0
        print(
            f"\nEXPECT loop (1 staged -> validated -> 0 pending): {'PASS' if passed else 'FAIL'}"
        )
        return 0 if passed else 1
    finally:
        d1.close()
        d3.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prove the D3 review + suggestion loop on synthetic D1 jobs (no SLM)."
    )
    parser.add_argument(
        "--store",
        default="review_check.sqlite",
        help="SQLite store path (D1 + D3 tables). Fresh each run; gitignored.",
    )
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.store))
