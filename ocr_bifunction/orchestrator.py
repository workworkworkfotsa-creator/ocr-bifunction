"""Batch orchestrator — the end-to-end backbone of the BATCH regime.

One entry point that runs a LOT of real documents through the proven per-document cores and
produces uniform records split into two piles — the ④ CENTRALISER / ⑤ REMONTER separation:

    auto    -> validated confidently, ready to centralise as clean records
    review  -> doubtful (human verdict, incomplete, unrecognised, or a RAG-lane unidentified
               doc) -> the queue a human must look at

It does NOT invent a new pipeline: each item is dispatched to the SAME core the API regime
uses — `process_ci_submission` for a CI upload, `route_document` (2-lane) for anything else —
so the two regimes share one set of stages (cf. CLAUDE.md). Escalation (LightOCR) is enabled
only for the CI verso, exactly as in the pipeline; the batch can afford it.

PERSISTENCE IS DELIBERATELY OUT OF SCOPE HERE. `process_batch` RETURNS a `BatchResult`
(records + the auto/review split); where those records land — a SQLite store, a JSON/CSV dump,
a MariaDB table on the IT side — is a separate SINK that plugs onto `BatchResult`. That seam is
the ④/⑤ contract to co-freeze with IT; this module stays storage-agnostic on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ocr_bifunction.pipeline import CiSubmissionResult, process_ci_submission
from ocr_bifunction.rag import Summary
from ocr_bifunction.reader import OcrEngine
from ocr_bifunction.router import RoutedDocument, route_document

# The document type that means "this upload is a CI" — dispatches to the recto+verso
# submission flow instead of single-document routing (mirrors the API's document_type key).
CI_CATEGORY = "carte_identite"


@dataclass
class BatchItem:
    """One unit of work in a batch: a single document, or a CI submission (several files).

    `document_type` is the optional declared category (like the API's hint). When it is
    `CI_CATEGORY` the item is a CI upload (its `paths` are the sides); otherwise the item is a
    single document and only `paths[0]` is used, scoped to that declared category (or all
    categories when None)."""

    paths: list[Path]
    document_type: str | None = None


@dataclass
class DocumentRecord:
    """The uniform per-document product of the batch, whatever lane produced it.

    `outcome` is the ④/⑤ decision — `auto` (centralise) or `review` (send to the human queue).
    `detail` keeps the lane-native status for the human (structured verdict, CI
    complete/incomplete/unrecognised, or "rag")."""

    source: str
    lane: str  # "ci" | "structured" | "rag"
    outcome: str  # "auto" | "review"
    detail: str | None = None
    category: str | None = None
    template_id: str | None = None
    fields: dict[str, str | None] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    summary: Summary | None = None  # rag lane only
    chunk_count: int = 0  # rag lane only


@dataclass
class BatchResult:
    """Every record from the batch, and the ④/⑤ split derived from each record's outcome.

    This is the SINK SEAM: a persistence step (SQLite / JSON / MariaDB) consumes `records`
    (or the `auto` / `review` views) — it is intentionally not decided here."""

    records: list[DocumentRecord] = field(default_factory=list)

    @property
    def auto(self) -> list[DocumentRecord]:
        """Records validated confidently — ready to centralise (④)."""
        return [record for record in self.records if record.outcome == "auto"]

    @property
    def review(self) -> list[DocumentRecord]:
        """The doubtful queue a human must look at (⑤ remonter)."""
        return [record for record in self.records if record.outcome == "review"]


def _record_from_routed(routed: RoutedDocument) -> DocumentRecord:
    # Only a structured doc that passed its single-doc validation is auto; a RAG-lane doc is
    # by construction unidentified -> always the human queue.
    is_auto = routed.lane == "structured" and routed.verdict == "auto"
    return DocumentRecord(
        source=routed.source,
        lane=routed.lane,
        outcome="auto" if is_auto else "review",
        detail=routed.verdict if routed.lane == "structured" else "rag",
        category=routed.category,
        template_id=routed.template_id,
        fields=routed.fields,
        reasons=routed.reasons,
        summary=routed.summary,
        chunk_count=routed.chunk_count,
    )


def _record_from_ci(result: CiSubmissionResult, item: BatchItem) -> DocumentRecord:
    source = ", ".join(path.name for path in item.paths)
    if result.status == "complete" and result.record is not None:
        record = result.record
        return DocumentRecord(
            source=source,
            lane="ci",
            outcome="auto" if record.verdict == "auto" else "review",
            detail="complete",
            category=item.document_type,
            template_id=record.template_id,
            fields=record.fields,
            reasons=record.reasons,
        )
    # incomplete (a side missing) or unrecognized -> the human queue.
    return DocumentRecord(
        source=source,
        lane="ci",
        outcome="review",
        detail=result.status,
        category=item.document_type,
        reasons=result.reasons,
    )


def process_document(
    item: BatchItem,
    templates_directory: Path,
    engine: OcrEngine,
    escalation_engine: OcrEngine | None = None,
    templates: list[dict] | None = None,
) -> DocumentRecord:
    """Dispatch one item to its core (CI submission or single-doc router) -> a record.

    `templates` injects the single-doc template list (the D2 store read path) instead of the
    JSON files; the CI submission flow still reads the directory (its templates are not in the
    suggestion/growth loop yet)."""
    if item.document_type == CI_CATEGORY:
        result = process_ci_submission(
            item.paths,
            engine,
            templates_directory,
            category=CI_CATEGORY,
            escalation_engine=escalation_engine,
        )
        return _record_from_ci(result, item)
    routed = route_document(
        item.paths[0],
        templates_directory,
        engine,
        category=item.document_type,
        templates=templates,
    )
    return _record_from_routed(routed)


def process_batch(
    items: list[BatchItem],
    templates_directory: Path,
    engine: OcrEngine,
    escalation_engine: OcrEngine | None = None,
    templates: list[dict] | None = None,
) -> BatchResult:
    """Run every item through its core and collect the uniform records (④/⑤ split included).

    Sequential on purpose: one SLM runs at a time on the 8 GB / no-GPU target, and escalation
    (LightOCR) is heavy — the batch regime tolerates the latency. Returns a `BatchResult`; the
    caller decides where the records are persisted (the sink seam). `templates` = the D2 read
    path (see process_document)."""
    return BatchResult(
        records=[
            process_document(
                item, templates_directory, engine, escalation_engine, templates
            )
            for item in items
        ]
    )
