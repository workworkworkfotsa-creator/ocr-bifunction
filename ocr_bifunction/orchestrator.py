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
an internal DB table on the IT side — is a separate SINK that plugs onto `BatchResult`. That seam is
the ④/⑤ contract to co-freeze with IT; this module stays storage-agnostic on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ocr_bifunction.pipeline import CiSubmissionResult, process_ci_submission
from ocr_bifunction.rag import Summary
from ocr_bifunction.reader import OcrEngine
from ocr_bifunction.router import RoutedDocument, SuggesterHook, route_document
from ocr_bifunction.suggestion import SuggestionOutcome
from ocr_bifunction.template import ValidationContext

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

    `outcome` is the routing decision: `auto` (centralise ④), `review` (human queue ⑤), or
    `reject` (PROVEN invalid — the anti-fraud verdict, auto-terminal, no human). `detail`
    keeps the lane-native status for the human (structured verdict, CI
    complete/incomplete/unrecognised, or "rag")."""

    source: str
    lane: str  # "ci" | "structured" | "rag"
    outcome: str  # "auto" | "review" | "reject"
    detail: str | None = None
    category: str | None = None
    template_id: str | None = None
    fields: dict[str, str | None] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    summary: Summary | None = None  # rag lane only
    chunk_count: int = 0  # rag lane only
    suggestion: SuggestionOutcome | None = None  # rag lane only, when a suggester fired
    missing: list[str] = field(
        default_factory=list
    )  # ci lane only: the side(s) an incomplete/unrecognized submission is missing
    verso_read_path: str | None = (
        None  # ci lane only: which read won the verso MRZ (raw|enhance|escalation) — the
        # escalation-provenance diagnostic the async worker folds into the row's reasons
    )


@dataclass
class BatchResult:
    """Every record from the batch, and the ④/⑤ split derived from each record's outcome.

    This is the SINK SEAM: a persistence step (SQLite / JSON / internal DB) consumes `records`
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

    @property
    def rejected(self) -> list[DocumentRecord]:
        """Records PROVEN invalid (anti-fraud verdict) — auto-terminal, no human review."""
        return [record for record in self.records if record.outcome == "reject"]


def _suggestion_reason(suggestion: SuggestionOutcome) -> str:
    """One human-readable line for the review queue, whatever the suggestion's fate."""
    if suggestion.verified:
        return (
            f"SLM suggests template '{suggestion.suggested_template_id}' "
            "(anchors confirmed + fit validated) — pending human validation"
        )
    if suggestion.suggested_template_id is None:
        return "SLM suggestion: UNKNOWN (no known template fits)"
    return (
        f"SLM suggested '{suggestion.suggested_template_id}' but it did not verify "
        "(hallucinated anchors or failed fit) — plain human review"
    )


def _record_from_routed(
    routed: RoutedDocument, declared_type: str | None = None
) -> DocumentRecord:
    # The verdict's own value IS the record outcome (auto/review/reject) — no mapping.
    if routed.lane == "structured" and routed.verdict is not None:
        outcome = routed.verdict.value
        detail = routed.verdict.value
        category = routed.category
        reasons = list(routed.reasons)
    else:
        # RAG lane: unidentified by construction -> the human review queue. Carry the
        # human-readable line + retrieval keywords BOTH entry points used to build by hand
        # (the API door, the async worker), so the shared intake layer reproduces them
        # instead of dropping them. `routed.category` is None on this lane -> keep the
        # caller's DECLARED type as the category so the nightly draft pass knows which
        # future category to cluster this unknown under.
        outcome = "review"
        detail = "rag"
        category = routed.category or declared_type
        reasons = [
            *routed.reasons,
            "non-structured document — routed to retrieval / human review",
        ]
        if routed.summary is not None and routed.summary.keywords:
            reasons.append("keywords: " + ", ".join(routed.summary.keywords))
    if routed.suggestion is not None:
        reasons.append(_suggestion_reason(routed.suggestion))
    return DocumentRecord(
        source=routed.source,
        lane=routed.lane,
        outcome=outcome,
        detail=detail,
        category=category,
        template_id=routed.template_id,
        fields=routed.fields,
        reasons=reasons,
        summary=routed.summary,
        chunk_count=routed.chunk_count,
        suggestion=routed.suggestion,
    )


def _record_from_ci(result: CiSubmissionResult, item: BatchItem) -> DocumentRecord:
    source = ", ".join(path.name for path in item.paths)
    if result.status == "complete" and result.record is not None:
        record = result.record
        return DocumentRecord(
            source=source,
            lane="ci",
            # A recto/verso identity mismatch is a reject (proven invalid); auto passes; an
            # unreliable read (failed checksums, nothing to compare) is review. The verdict's
            # own value IS the outcome (auto/review/reject).
            outcome=record.verdict.value,
            detail="complete",
            category=item.document_type,
            template_id=record.template_id,
            fields=record.fields,
            reasons=record.reasons,
            verso_read_path=record.verso_read_path,
        )
    # incomplete (a side missing) or unrecognized -> the human queue. `missing` names the
    # side(s) to ask the uploader for (the wire contract's `missing`, kept on the record).
    return DocumentRecord(
        source=source,
        lane="ci",
        outcome="review",
        detail=result.status,
        category=item.document_type,
        reasons=result.reasons,
        missing=result.missing,
    )


def process_document(
    item: BatchItem,
    templates_directory: Path,
    engine: OcrEngine,
    escalation_engine: OcrEngine | None = None,
    templates: list[dict] | None = None,
    suggester: SuggesterHook | None = None,
    context: ValidationContext | None = None,
    today: date | None = None,
) -> DocumentRecord:
    """Dispatch one item to its core (CI submission or single-doc router) -> a record.

    `templates` injects the single-doc template list (the D2 store read path) instead of the
    JSON files; the CI submission flow still reads the directory (its templates are not in the
    suggestion/growth loop yet). `suggester` (opt-in, like escalation) wakes the SLM on a
    single-doc no-match; the outcome rides on the record for the sink to stage into D3.
    `context`/`today` feed the anti-fraud validation (the context checks + date freshness)."""
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
        suggester=suggester,
        context=context,
        today=today,
    )
    return _record_from_routed(routed, declared_type=item.document_type)


def process_batch(
    items: list[BatchItem],
    templates_directory: Path,
    engine: OcrEngine,
    escalation_engine: OcrEngine | None = None,
    templates: list[dict] | None = None,
    suggester: SuggesterHook | None = None,
    context: ValidationContext | None = None,
    today: date | None = None,
) -> BatchResult:
    """Run every item through its core and collect the uniform records (split included).

    Sequential on purpose: one SLM runs at a time on the 8 GB / no-GPU target, and escalation
    (LightOCR) is heavy — the batch regime tolerates the latency. Returns a `BatchResult`; the
    caller decides where the records are persisted (the sink seam). `templates` = the D2 read
    path; `suggester` = the opt-in SLM suggestion hook (see process_document); `context`/`today`
    feed the anti-fraud validation."""
    return BatchResult(
        records=[
            process_document(
                item,
                templates_directory,
                engine,
                escalation_engine,
                templates,
                suggester,
                context,
                today,
            )
            for item in items
        ]
    )
