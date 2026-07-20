"""Intake — one document-handling module both entry points funnel through.

Until now "one document + its context -> a persisted outcome" was authored THREE times: inline
in the API door (`_handle_ci_submission`/`_handle_single_document`), inline in the async worker
(`_process_ci_job`/`_process_routed_job`), and — persistence-agnostic and unused in production —
in `orchestrator.process_document`. The dispatch (CI vs routed), the non-conformity reaction, the
declared-vs-recognized type-mismatch and the record->Job mapping were duplicated door<->worker.

This is the single application/intake layer. It sits ON TOP of the PURE `orchestrator.process_document`
(which stays "document -> DocumentRecord", no persistence) and adds the parts that were duplicated:

  - type-mismatch: a doc that matches no template of its DECLARED type but matches ANOTHER
    category's template is a proven non-conformity ("attendu X, reçu Y"), not an unknown;
  - non-conformity reaction: a `reject` verdict (or a type-mismatch) obeys the métier's per-category
    conformity policy — block/block_holder -> rejected, flag_and_continue -> needs_review flagged;
  - the record->Job mapping (`job_from_outcome`), single-sourced.

`handle_document` is a PURE function: it touches NO store. The adapters persist the returned
`DocumentOutcome` — the door `save`s a new row, the worker `update_status`es the claimed row —
so the durable checkpoints (and crash recovery via the D1 row + `recover_stale`) stay in the
adapters, and the handler is unit-testable on an in-memory Store with no FastAPI or subprocess.

Two edges stay in the adapters on purpose (real per-entry-point policy, NOT duplication): the door
defers a doubtful CI to the escalation lane (it is the fast lane) and done-traces an
incomplete/unrecognized CI (the uploader must resubmit — there is no reviewer fix); the worker,
having no uploader to bounce back to, routes those to needs_review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ocr_bifunction.conformity_policy import (
    CONFORMITY_ACTION_FLAG_AND_CONTINUE,
    ConformityPolicy,
    resolve_conformity_action,
)
from ocr_bifunction.orchestrator import (
    BatchItem,
    DocumentRecord,
    process_document,
)
from ocr_bifunction.reader import OcrEngine
from ocr_bifunction.repository import Job
from ocr_bifunction.router import route_document
from ocr_bifunction.status import STATUS_NEEDS_REVIEW, STATUS_REJECTED
from ocr_bifunction.template import ValidationContext
from ocr_bifunction.verdict import Verdict


@dataclass
class DocumentOutcome:
    """The persisted-outcome-in-waiting: what the record BECOMES once the intake rules ran.

    `record` is the pure processing product (`orchestrator.DocumentRecord`). `status` is the D1
    row state the general case lands in (a Verdict.d1_status, or the non-conformity reaction's
    result); `verdict` is the Verdict.value string (or None for a trace). `retain_bytes` says the
    evidence must be spooled (needs_review docs the reviewer/drafting reads; retained proof of a
    non-conformity). `nonconformity` marks that a PROVEN non-conformity drove the status.

    The adapters persist this: the door builds a NEW Job (`job_from_outcome`), the worker
    `update_status`es the claimed row from these fields."""

    record: DocumentRecord
    status: str
    verdict: str | None
    reasons: list[str] = field(default_factory=list)
    retain_bytes: bool = False
    nonconformity: bool = False


def _is_unrecognized_under_declared(record: DocumentRecord) -> bool:
    """The doc matched no structured template of its declared type: a RAG-lane routed doc, or a
    CI upload that produced no CI template + no MRZ (`unrecognized`). The type-mismatch check
    only makes sense here — a structured/auto match already recognized the doc."""
    return record.lane == "rag" or (
        record.lane == "ci" and record.detail == "unrecognized"
    )


def _type_mismatch_outcome(
    item: BatchItem,
    templates_directory: Path,
    engine: OcrEngine,
    templates: list[dict] | None,
    context: ValidationContext | None,
    today: date | None,
    conformity_policies: dict[str, ConformityPolicy],
) -> DocumentOutcome | None:
    """Re-route the doc against EVERY category (declared type dropped): a structured match under
    a DIFFERENT category is a proven non-conformity. Costs a second read, exactly as the door's
    old `_detected_type_mismatch` (and the worker never did this — the gap this closes)."""
    declared = item.document_type
    rerouted = route_document(
        item.paths[0],
        templates_directory,
        engine,
        category=None,
        templates=templates,
        context=context,
        today=today,
    )
    if not (
        rerouted.lane == "structured"
        and rerouted.category
        and rerouted.category != declared
    ):
        return None
    reasons = [
        f"type mismatch: declared '{declared}', recognized '{rerouted.category}' "
        f"(template {rerouted.template_id})"
    ]
    mismatch_record = DocumentRecord(
        source=", ".join(path.name for path in item.paths),
        lane="structured",
        outcome=Verdict.REJECT.value,
        detail="type_mismatch",
        category=rerouted.category,
        template_id=rerouted.template_id,
        fields=rerouted.fields,
        reasons=reasons,
    )
    return _nonconformity_outcome(
        mismatch_record, declared, rerouted.category, reasons, conformity_policies
    )


def _nonconformity_outcome(
    record: DocumentRecord,
    declared_type: str | None,
    category: str | None,
    reasons: list[str],
    conformity_policies: dict[str, ConformityPolicy],
) -> DocumentOutcome:
    """The métier's REACTION to a proven non-conformity, resolved on the DECLARED type first
    (a passport sent as a CI is a 'carte_identite' incident): flag_and_continue -> needs_review
    flagged; block/block_holder -> rejected. Both RETAIN the evidence for the review/compliance."""
    action = resolve_conformity_action(declared_type or category, conformity_policies)
    if action == CONFORMITY_ACTION_FLAG_AND_CONTINUE:
        return DocumentOutcome(
            record=record,
            status=STATUS_NEEDS_REVIEW,
            verdict=Verdict.REVIEW.value,
            reasons=["non-conformity FLAGGED (policy: process continues)", *reasons],
            retain_bytes=True,
            nonconformity=True,
        )
    return DocumentOutcome(
        record=record,
        status=STATUS_REJECTED,
        verdict=Verdict.REJECT.value,
        reasons=reasons,
        retain_bytes=True,
        nonconformity=True,
    )


def handle_document(
    item: BatchItem,
    templates_directory: Path,
    engine: OcrEngine,
    *,
    escalation_engine: OcrEngine | None = None,
    templates: list[dict] | None = None,
    context: ValidationContext | None = None,
    today: date | None = None,
    conformity_policies: dict[str, ConformityPolicy] | None = None,
) -> DocumentOutcome:
    """Process one document to a persist-ready outcome — the flow both entry points share.

    Runs the pure core (`process_document`: CI submission or 2-lane router), then applies the
    type-mismatch check and the non-conformity reaction. Returns a `DocumentOutcome`; it does NOT
    persist. `escalation_engine` arms the heavy CI verso re-read (the worker passes it; the door
    passes None and defers a doubtful CI itself). `conformity_policies` drives the reaction."""
    conformity_policies = conformity_policies or {}
    record = process_document(
        item,
        templates_directory,
        engine,
        escalation_engine,
        templates,
        context=context,
        today=today,
    )

    if item.document_type is not None and _is_unrecognized_under_declared(record):
        mismatch = _type_mismatch_outcome(
            item,
            templates_directory,
            engine,
            templates,
            context,
            today,
            conformity_policies,
        )
        if mismatch is not None:
            return mismatch

    if record.outcome == Verdict.REJECT.value:
        return _nonconformity_outcome(
            record,
            item.document_type,
            record.category,
            record.reasons,
            conformity_policies,
        )

    verdict = Verdict(record.outcome)
    return DocumentOutcome(
        record=record,
        status=verdict.d1_status,
        verdict=verdict.value,
        reasons=record.reasons,
        # A needs_review doc keeps its bytes: the reviewer sees it next to the extraction and
        # the nightly draft pass clusters the unknowns. An auto (done) doc needs none.
        retain_bytes=verdict is Verdict.REVIEW,
    )


def job_from_outcome(
    outcome: DocumentOutcome,
    *,
    source: str | None = None,
    request_id: str | None = None,
    document_ref: str | None = None,
    expected_holder_name: str | None = None,
    execution_lane: str = "fast",
    use_case: str | None = None,
) -> Job:
    """The single record->Job mapping (was hand-assembled ~7x in the HTTP handlers). `source`
    overrides the record's temp-file name with the upload's real name; `document_ref` is the
    spool pointer when `outcome.retain_bytes`. `use_case` is a snapshot of the API-key
    resolution (use_case_key.py) at intake time — traceability, not yet a behaviour fork."""
    record = outcome.record
    return Job(
        source=source if source is not None else record.source,
        category_lane=record.lane,
        status=outcome.status,
        execution_lane=execution_lane,
        verdict=outcome.verdict,
        category=record.category,
        template_id=record.template_id,
        record_fields=record.fields,
        reasons=outcome.reasons,
        request_id=request_id,
        document_ref=document_ref,
        expected_holder_name=expected_holder_name,
        use_case=use_case,
    )
