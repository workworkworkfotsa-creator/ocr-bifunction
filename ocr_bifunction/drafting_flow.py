"""The DRAFT pass — the drafting lane wired INTO the flow (nightly watchdog step).

Until now the deterministic drafting lane (D-a clustering, D-b invariance drafts) lived
in a CLI runner fed with file paths. This module runs it FROM THE STORES: the unknowns
that accumulated in D1 as `needs_review` (no template matched) keep their bytes in the
spool (`document_ref`), so the nightly pass can read them back, cluster the layouts that
RETURN, draft a template per cluster, decorate it (D-c: deterministic candidate checks,
optional SLM field naming) and stage the draft as a D3 pending suggestion — where the
review page already shows it and the human ticks + validates (promotion D2, re-match).

Guards, in the lane's own doctrine:
  - deterministic first — the SLM only renames fields, opt-in, and a dead llama-swap
    degrades to placeholders with a recorded reason (never blocks the pass);
  - the OCR gate stays mechanical: image-only unknowns are SKIPPED unless the caller
    armed an engine (the shared-machine brake, same contract as draft_check --ocr);
  - idempotent night after night: a cluster with a suggestion already staged (pending
    or decided) on one of its jobs is skipped;
  - CI jobs never enter (a CI pair is reconciled, not template-drafted).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ocr_bifunction.drafting import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DraftingDocument,
    cluster_unknown_documents,
    draft_from_cluster,
    seed_candidate_checks,
)
from ocr_bifunction.field_naming import _draft_retests_green, name_draft_fields
from ocr_bifunction.reader import OcrEngine, read_document
from ocr_bifunction.repository import STATUS_NEEDS_REVIEW, Job, Repository
from ocr_bifunction.review_repository import Review, ReviewRepository, Suggestion
from ocr_bifunction.template_repository import TemplateRepository

# Fallback category for a cluster whose jobs declared nothing (or disagreed): the
# reviewer sees an explicit "to be categorized", never a silent guess.
UNCATEGORIZED = "a_categoriser"


@dataclass
class DraftPassReport:
    """What one DRAFT pass did — staged drafts and every skip, with its reason."""

    staged_template_ids: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _readable_unknowns(
    jobs: list[Job], engine: OcrEngine | None, report: DraftPassReport
) -> list[tuple[DraftingDocument, Job]]:
    """Read back the drafting matter: needs_review jobs with retained bytes and no
    matched template. Image-only documents are skipped unless an engine was armed."""
    readable: list[tuple[DraftingDocument, Job]] = []
    for job in jobs:
        if job.category_lane == "ci" or job.template_id is not None:
            continue
        spool_directory = Path(job.document_ref) if job.document_ref else None
        if spool_directory is None or not spool_directory.is_dir():
            report.skipped.append(
                f"job #{job.job_id} ({job.source}): no retained document"
            )
            continue
        files = sorted(path for path in spool_directory.iterdir() if path.is_file())
        if not files:
            report.skipped.append(f"job #{job.job_id} ({job.source}): empty spool")
            continue
        result = read_document(files[0], engine)
        if not result.text.strip():
            report.skipped.append(
                f"job #{job.job_id} ({job.source}): no extractable text "
                "(image-only; arm --draft-ocr to OCR it)"
            )
            continue
        readable.append(
            (
                DraftingDocument(
                    source=job.source, text=result.text, lines=result.lines
                ),
                job,
            )
        )
    return readable


def _cluster_category(jobs: list[Job]) -> str:
    declared = {job.category for job in jobs if job.category}
    if len(declared) == 1:
        return declared.pop()
    return UNCATEGORIZED


def _free_draft_template_id(
    category: str, template_repository: TemplateRepository
) -> str:
    for number in range(1, 100):
        candidate = f"draft_{category}_{number:02d}"
        if template_repository.get(candidate) is None:
            return candidate
    raise RuntimeError(f"no free draft template id for category {category!r}")


def run_draft_pass(
    repository: Repository,
    review_repository: ReviewRepository,
    template_repository: TemplateRepository,
    *,
    engine: OcrEngine | None = None,
    slm_naming: bool = False,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> DraftPassReport:
    """One DRAFT pass over the accumulated unknowns; returns what it staged/skipped.

    `engine` arms OCR for image-only unknowns (None = skip them, the mechanical brake).
    `slm_naming` wakes granite to name the placeholder fields — failure degrades to
    placeholders with a reason, the pass never depends on the machine being free."""
    report = DraftPassReport()
    jobs = repository.pending(STATUS_NEEDS_REVIEW)
    documents_with_jobs = _readable_unknowns(jobs, engine, report)
    if len(documents_with_jobs) < 2:
        return report

    job_by_source = {document.source: job for document, job in documents_with_jobs}
    clusters = cluster_unknown_documents(
        [document for document, _ in documents_with_jobs],
        similarity_threshold=similarity_threshold,
    )
    for cluster in clusters:
        if len(cluster) < 2:
            continue  # a one-off stays RAG material
        cluster_jobs = [job_by_source[document.source] for document in cluster]
        already_staged = [
            job.job_id
            for job in cluster_jobs
            if (review := review_repository.by_job(job.job_id)) is not None
            and review.suggestion is not None
        ]
        if already_staged:
            report.skipped.append(
                f"cluster of {len(cluster)}: suggestion already staged "
                f"(job #{already_staged[0]}) — idempotent skip"
            )
            continue

        category = _cluster_category(cluster_jobs)
        template_id = _free_draft_template_id(category, template_repository)
        draft_report = draft_from_cluster(cluster, category, template_id)
        if draft_report.template is None:
            report.skipped.append(
                f"cluster of {len(cluster)}: draft rejected "
                f"({'; '.join(draft_report.reasons)})"
            )
            continue

        # D-c part 2 (deterministic): candidate value checks from the cluster's own
        # extractions, kept only if the unchanged D-b gate re-tests green.
        draft = draft_report.template
        seeded = seed_candidate_checks(draft, draft_report.extractions_by_source)
        seeding_failures = _draft_retests_green(seeded, cluster)
        if not seeding_failures:
            draft = seeded
        else:
            report.skipped.append(
                f"{template_id}: candidate checks dropped, re-test failed "
                f"({'; '.join(seeding_failures)})"
            )

        # D-c part 1 (opt-in): SLM names the placeholders; any failure -> placeholders.
        if slm_naming:
            try:
                naming = name_draft_fields(draft, cluster)
                draft = naming.template
                if naming.reasons:
                    report.skipped.append(
                        f"{template_id}: naming degraded ({'; '.join(naming.reasons)})"
                    )
            except Exception as error:
                report.skipped.append(
                    f"{template_id}: SLM naming unavailable "
                    f"({type(error).__name__}) — placeholders kept"
                )

        first_job = cluster_jobs[0]
        review = review_repository.by_job(first_job.job_id)
        if review is None:
            review_id = review_repository.open_review(
                Review(
                    job_id=first_job.job_id,
                    projection={
                        "source": first_job.source,
                        "lane": first_job.category_lane,
                        "verdict": first_job.verdict,
                    },
                )
            )
        else:
            review_id = review.review_id
        review_repository.stage_suggestion(
            review_id,
            Suggestion(
                template_id=draft["template_id"],
                category=draft.get("category"),
                anchors=list(draft.get("match", {}).get("all_anchors", [])),
                template=draft,
            ),
        )
        report.staged_template_ids.append(draft["template_id"])
    return report
