"""THE DOOR — one upload in, one stable envelope out.

Holds the whole sync lane: idempotency, admission, the two door edges (a missing CI
side becomes a trace row; a complete-but-doubtful CI defers to the watchdog), and the
job-polling endpoints. `_handle_validated_document` is the single sync processing
path — everything above it decides WHEN, it decides nothing itself beyond the edges."""

from __future__ import annotations

import base64
import binascii
import tempfile
from datetime import date
from pathlib import Path

from fastapi import Header, HTTPException, Response
from fastapi.responses import FileResponse

from ocr_bifunction.governance.execution_policy import (
    EXECUTION_LANE_FOR_ASYNC_MODE,
    EXECUTION_MODE_SYNC,
    resolve_execution,
)
from ocr_bifunction.governance.capacity_settings import (
    OVERFLOW_ACTION_REJECT_503,
)
from ocr_bifunction.governance.conformity_policy import (
    CONFORMITY_ACTION_BLOCK_HOLDER,
    resolve_conformity_action,
)
from ocr_bifunction.knowledge.context_assembly import (
    collect_validated_attestations,
)
from ocr_bifunction.flow.intake import handle_document, job_from_outcome
from ocr_bifunction.flow.orchestrator import BatchItem
from ocr_bifunction.reading.reader import IMAGE_SUFFIXES
from ocr_bifunction.storage.repository import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_RECEIVED,
    STATUS_REJECTED,
    Job,
)
from ocr_bifunction.extraction.template import (
    ValidationContext,
)
from ocr_bifunction.validation.verdict import Verdict
from ocr_bifunction.governance.use_case_key import (
    resolve_use_case,
)

from fastapi import APIRouter

from ocr_bifunction.adapters.api_maquette.contract import (
    JobResponse,
    ValidateRequest,
    ValidateResponse,
    _http_status_for,
)
from ocr_bifunction.adapters.api_maquette.settings import (
    PAGE_RENDER_DPI,
    TEMPLATES_DIRECTORY,
)
from ocr_bifunction.adapters.api_maquette.spool import (
    _spool_files,
    _spooled_document_files,
    _write_files,
)
from ocr_bifunction.adapters.api_maquette.store_access import (
    _ensure_conformity_policy_repository,
    _ensure_execution_policy_repository,
    _ensure_issuer_registry_repository,
    _ensure_repository,
    _ensure_review_repository,
    _ensure_template_repository,
    _ensure_use_case_key_repository,
    _get_engine,
    _get_job_row,
    _load_capacity_settings,
    _release_sync_slot,
    _repository_lock,
    _save_job,
    _try_acquire_sync_slot,
)

router = APIRouter()

# Idempotency cache keyed by request_id: a replay returns the same result (same job_id). Stays
# in memory — it dedupes the whole response envelope, not just persisted jobs (out of D1 scope).
# BOUNDED: an unbounded dict is a slow leak under load; beyond the cap the OLDEST entry is
# evicted (dict preserves insertion order). A late replay of an evicted id simply reprocesses.
_IDEMPOTENCY_CACHE_MAX_ENTRIES = 1024
_idempotency_cache: dict[str, "ValidateResponse"] = {}


def _cache_idempotent_response(request_id: str, wire: "ValidateResponse") -> None:
    if request_id in _idempotency_cache:
        return
    while len(_idempotency_cache) >= _IDEMPOTENCY_CACHE_MAX_ENTRIES:
        _idempotency_cache.pop(next(iter(_idempotency_cache)))
    _idempotency_cache[request_id] = wire


def _spool_and_enqueue(
    files: list[tuple[str, bytes]],
    category: str | None,
    fast_reasons: list[str],
    request_id: str | None,
    *,
    category_lane: str = "ci",
    execution_lane: str = "escalation",
    expected_holder_name: str | None = None,
    use_case: str | None = None,
) -> int:
    """Persist the uploaded bytes to the spool and insert a `received` D1 row pointing at them.

    That row IS the queue entry (the status column is the signal): the watchdog worker — a
    separate process — claims it, processes the spooled files (`document_ref`), writes the
    terminal state and purges the spool directory (PII hygiene). Two producers use this door:
    the CI doubt escalation (defaults) and the execution policy's async modes
    (category_lane 'unrouted', execution_lane 'deferred'/'nightly')."""
    source = ", ".join(filename for filename, _ in files)
    return _save_job(
        Job(
            source=source,
            category_lane=category_lane,
            status=STATUS_RECEIVED,
            execution_lane=execution_lane,
            category=category,
            reasons=fast_reasons,  # WHY it waits (doubt or policy), visible while pending
            request_id=request_id,
            document_ref=_spool_files(files),
            expected_holder_name=expected_holder_name,
            use_case=use_case,
        )
    )


# --- Fast path: decode + run the submission with no escalation, in the request. ------


def _decode_files(request: ValidateRequest) -> list[tuple[str, bytes]]:
    """Decode the uploaded files. Raises HTTPException(400) on a bad/empty request."""
    if not request.files:
        raise HTTPException(status_code=400, detail="files must not be empty")
    decoded: list[tuple[str, bytes]] = []
    for upload in request.files:
        try:
            data = base64.b64decode(upload.content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HTTPException(
                status_code=400,
                detail=f"invalid base64 for {upload.filename!r}: {error}",
            )
        if not data:
            raise HTTPException(
                status_code=400, detail=f"empty content for {upload.filename!r}"
            )
        decoded.append((upload.filename, data))
    return decoded


# --- Validation context + holder block: what the shared handler and the door edges read. --


def _build_validation_context(
    expected_holder_name: str | None = None,
) -> ValidationContext:
    """Assemble what the context-dependent anti-fraud checks read: the curated issuer
    registry (None when empty — fail-loud review, never an empty false proof), the
    DECLARED holder (manual entry; feeds reconcile_ci), and the validated attestations
    on file — projected through each template's MÉTIER-configured
    `attestation_reference_roles` block (corroborated_by)."""
    with _repository_lock:
        identifiers = _ensure_issuer_registry_repository().identifiers()
        attestations = collect_validated_attestations(
            _ensure_repository(), _ensure_template_repository().active_templates()
        )
    return ValidationContext(
        ci_reference_name=expected_holder_name,
        issuer_registry=identifiers,
        validated_attestations=attestations,
    )


def _holder_block_reason(expected_holder_name: str | None) -> str | None:
    """The block_holder guard: an OPEN non-conformity (rejected row, no review decision
    yet) for the same DECLARED holder, in a category whose policy blocks the dossier,
    refuses subsequent uploads until the review clears it."""
    if not expected_holder_name:
        return None
    with _repository_lock:
        rejected_jobs = _ensure_repository().pending(STATUS_REJECTED)
        review_repository = _ensure_review_repository()
        conformity_policies = {
            policy.category: policy
            for policy in _ensure_conformity_policy_repository().all_policies()
        }
        for job in rejected_jobs:
            if job.expected_holder_name != expected_holder_name:
                continue
            if job.document_ref is None:
                # No retained evidence = a refused-while-blocked TRACE row, not an
                # examined non-conformity — it must never keep the block alive itself.
                continue
            action = resolve_conformity_action(job.category, conformity_policies)
            if action != CONFORMITY_ACTION_BLOCK_HOLDER:
                continue
            review = review_repository.by_job(job.job_id)
            if review is None or review.decision is None:  # still OPEN
                return (
                    f"dossier blocked: open non-conformity (job #{job.job_id}) for "
                    "this declared holder — clear it at the review to unblock"
                )
    return None


def _handle_validated_document(
    files: list[tuple[str, bytes]],
    document_type: str | None,
    request_id: str | None,
    expected_holder_name: str | None = None,
    use_case: str | None = None,
) -> ValidateResponse:
    """The single SYNC processing path — the shared intake handler + the door's two edges.

    `intake.handle_document` runs the pure core (CI submission or 2-lane router), the
    type-mismatch check and the non-conformity reaction, returning a persist-ready
    `DocumentOutcome`. The door then applies its two entry-point-specific edges (real
    policy, not duplication) and persists:

      (b) a CI with a side missing / not a CI at all -> a terminal `done` TRACE row
          (verdict None, no spool): the uploader must resubmit — no reviewer can fix a
          side that never arrived. `missing` names the side(s) to ask for.
      (a) a COMPLETE but doubtful CI -> the fast lane DEFERS it to the escalation worker
          (a heavier VLM verso re-read off the request path), answered 202 pending.

    Everything else (auto, a structured/rag review, a proven non-conformity) is persisted
    from the outcome via the single `job_from_outcome` mapping, its bytes retained iff the
    outcome says so (a needs_review doc the reviewer sees, a non-conformity's evidence).
    """
    with tempfile.TemporaryDirectory(prefix="ocr_bifunction_api_") as temp_directory:
        item = BatchItem(
            paths=_write_files(files, Path(temp_directory)),
            document_type=document_type,
        )
        with _repository_lock:
            active_templates = _ensure_template_repository().active_templates()
            conformity_policies = {
                policy.category: policy
                for policy in _ensure_conformity_policy_repository().all_policies()
            }
        context = _build_validation_context(expected_holder_name)
        try:
            outcome = handle_document(
                item,
                TEMPLATES_DIRECTORY,
                _get_engine(),
                escalation_engine=None,  # the door defers a doubtful CI (edge a) itself
                templates=active_templates,
                context=context,
                today=date.today(),
                conformity_policies=conformity_policies,
            )
        except Exception as error:  # surface a pipeline/engine crash as 5xx, don't hide
            raise HTTPException(
                status_code=500, detail=f"pipeline failure: {type(error).__name__}"
            )

    # `item.paths` are TEMP files (removed above) — the row must carry the upload's REAL
    # names, so `source` overrides the record's temp-file name in every branch below.
    source = ", ".join(filename for filename, _ in files)
    record = outcome.record

    # EDGE (b): a missing CI side / not-a-CI upload -> a done trace, no spool.
    if record.detail in ("incomplete", "unrecognized"):
        trace_reasons = list(record.reasons)
        if record.missing:
            trace_reasons.append(f"missing side(s): {', '.join(record.missing)}")
        job_id = _save_job(
            Job(
                source=source,
                category_lane="ci",
                status=STATUS_DONE,
                execution_lane="fast",
                category=document_type,
                reasons=trace_reasons,
                request_id=request_id,
                use_case=use_case,
            )
        )
        return ValidateResponse(
            status=record.detail,  # type: ignore[arg-type]  ("incomplete" | "unrecognized")
            verdict=None,
            reasons=record.reasons,
            missing=record.missing,
            job_id=job_id,
        )

    # EDGE (a): a complete but doubtful CI -> spool the bytes, enqueue for the watchdog.
    if record.lane == "ci" and outcome.verdict == Verdict.REVIEW.value:
        job_id = _spool_and_enqueue(
            files, document_type, outcome.reasons, request_id, use_case=use_case
        )
        return ValidateResponse(
            status="pending", verdict=None, reasons=outcome.reasons, job_id=job_id
        )

    # General case: persist the outcome as a new D1 row (single record->Job mapping).
    job = job_from_outcome(
        outcome,
        source=source,
        request_id=request_id,
        document_ref=_spool_files(files) if outcome.retain_bytes else None,
        expected_holder_name=expected_holder_name,
        use_case=use_case,
    )
    job_id = _save_job(job)
    return ValidateResponse(
        status=Verdict(outcome.verdict).wire_status,  # type: ignore[arg-type]
        verdict=outcome.verdict,  # type: ignore[arg-type]
        reasons=outcome.reasons,
        job_id=job_id,
    )


# --- Endpoints ------------------------------------------------------------------------


@router.post("/v1/documents:validate", response_model=ValidateResponse)
def validate_document(
    request: ValidateRequest,
    response: Response,
    x_ocr_api_key: str | None = Header(default=None, alias="X-OCR-Api-Key"),
) -> ValidateResponse:
    """Validate one upload: the EXECUTION POLICY decides WHEN, `document_type` decides WHICH flow.

    First the execution policy for the category (fallback '*') resolves sync vs async,
    honoring the caller's optional `processing_mode` hint only where the policy allows
    override. Async -> spool + a `received` D1 row in the mode's lane ('deferred' drained
    continuously, 'nightly' drained by the night pass), answered 202 pending. Sync ->
    `carte_identite` runs the CI submission flow; any other type (facture, …) or none runs
    a single document through the 2-lane router.

    `X-OCR-Api-Key` resolves which consumer profile (`use_case`) this request belongs to
    (use_case_key.py). Absent -> the default profile, SILENTLY (zero regression for every
    caller predating this header). Present but unknown/revoked -> 401 (a real auth
    guarantee, never a silent fallback).
    """
    with _repository_lock:
        use_case_key_repository = _ensure_use_case_key_repository()
    resolved_use_case = resolve_use_case(x_ocr_api_key, use_case_key_repository)
    if resolved_use_case.use_case is None:
        raise HTTPException(status_code=401, detail="invalid or unknown API key")

    if request.request_id and request.request_id in _idempotency_cache:
        cached = _idempotency_cache[request.request_id]
        response.status_code = _http_status_for(cached)
        return cached

    files = _decode_files(request)  # may raise 400

    # The block_holder guard fires BEFORE any processing: an open non-conformity of a
    # dossier whose policy blocks it refuses this upload outright (trace row, no spool
    # — the document was never examined).
    block_reason = _holder_block_reason(request.expected_holder_name)
    if block_reason is not None:
        job_id = _save_job(
            Job(
                source=", ".join(filename for filename, _ in files),
                category_lane="unrouted",
                status=STATUS_REJECTED,
                execution_lane="fast",
                verdict="reject",
                category=request.document_type,
                reasons=[block_reason],
                request_id=request.request_id,
                expected_holder_name=request.expected_holder_name,
                use_case=resolved_use_case.use_case,
            )
        )
        wire = ValidateResponse(
            status="rejected", verdict="reject", reasons=[block_reason], job_id=job_id
        )
        if request.request_id:
            _cache_idempotent_response(request.request_id, wire)
        response.status_code = _http_status_for(wire)
        return wire

    with _repository_lock:
        policies = {
            policy.category: policy
            for policy in _ensure_execution_policy_repository().all_policies()
        }
    resolved = resolve_execution(
        request.document_type, request.processing_mode, policies
    )

    if resolved.execution_mode != EXECUTION_MODE_SYNC:
        # Policy says async: no processing in the request. The bytes wait in the spool;
        # the row's lane says which drain schedule picks it up.
        job_id = _spool_and_enqueue(
            files,
            request.document_type,
            resolved.reasons,
            request.request_id,
            category_lane=(
                "ci" if request.document_type == "carte_identite" else "unrouted"
            ),
            execution_lane=EXECUTION_LANE_FOR_ASYNC_MODE[resolved.execution_mode],
            expected_holder_name=request.expected_holder_name,
            use_case=resolved_use_case.use_case,
        )
        wire = ValidateResponse(
            status="pending", verdict=None, reasons=resolved.reasons, job_id=job_id
        )
    else:
        # ADMISSION CONTROL: the sync lane is capped (SYNC_CONCURRENCY_LIMIT, a live
        # lever). A saturated door never melts — it applies the overflow action:
        # defer (the bi-mode valve: 202 pending on the 'deferred' lane) or reject_503.
        capacity = _load_capacity_settings()
        if not _try_acquire_sync_slot(capacity.sync_concurrency_limit):
            if capacity.sync_overflow_action == OVERFLOW_ACTION_REJECT_503:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "sync capacity saturated "
                        f"({capacity.sync_concurrency_limit} in flight) — retry"
                    ),
                    headers={"Retry-After": "5"},
                )
            overflow_reasons = [
                *resolved.reasons,
                (
                    "sync capacity saturated "
                    f"({capacity.sync_concurrency_limit} in flight) — deferred per "
                    "overflow policy"
                ),
            ]
            job_id = _spool_and_enqueue(
                files,
                request.document_type,
                overflow_reasons,
                request.request_id,
                category_lane=(
                    "ci" if request.document_type == "carte_identite" else "unrouted"
                ),
                execution_lane="deferred",
                expected_holder_name=request.expected_holder_name,
                use_case=resolved_use_case.use_case,
            )
            wire = ValidateResponse(
                status="pending", verdict=None, reasons=overflow_reasons, job_id=job_id
            )
        else:
            try:
                # ONE sync path — the shared intake handler dispatches CI vs single doc
                # INSIDE handle_document; the door only applies its two edges afterward.
                wire = _handle_validated_document(
                    files,
                    request.document_type,
                    request.request_id,
                    request.expected_holder_name,
                    use_case=resolved_use_case.use_case,
                )  # may raise 500
            finally:
                _release_sync_slot()
            if resolved.reasons:
                wire.reasons = [
                    *wire.reasons,
                    *resolved.reasons,
                ]  # e.g. an ignored hint, traced

    if request.request_id:
        _cache_idempotent_response(request.request_id, wire)
    response.status_code = _http_status_for(wire)
    return wire


@router.get("/v1/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: int) -> JobResponse:
    """Poll an escalation job by its D1 row id. 404 if unknown. Maps the D1 lifecycle
    (received/processing/done/needs_review/rejected/failed) to the client's pending|done view
    — every terminal state reads as `done` (the async work is finished; `verdict` says auto vs
    human vs reject)."""
    job = _get_job_row(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    client_status = (
        "done"
        if job.status
        in (STATUS_DONE, STATUS_NEEDS_REVIEW, STATUS_REJECTED, STATUS_FAILED)
        else "pending"
    )
    return JobResponse(
        status=client_status,  # type: ignore[arg-type]
        verdict=job.verdict,  # type: ignore[arg-type]
        reasons=job.reasons,
    )


@router.get("/v1/jobs/{job_id}/document")
def job_document(job_id: int, index: int = 0) -> FileResponse:
    """Serve one retained document file of a job (the review page's preview). Only jobs
    whose spool is still on disk have one — needs_review keeps it, terminal states purge
    it. `index` picks a file for multi-file submissions (e.g. a CI pair)."""
    job = _get_job_row(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    files = _spooled_document_files(job)
    if not files or index < 0 or index >= len(files):
        raise HTTPException(
            status_code=404, detail=f"no retained document for job {job_id}"
        )
    return FileResponse(files[index])


@router.get("/v1/jobs/{job_id}/page")
def job_page(job_id: int, index: int = 0, page: int = 0) -> Response:
    """Render ONE page of a retained document to PNG — what the highlight overlay needs.

    A browser's built-in PDF viewer is an opaque plugin: nothing can be drawn over it, its
    scroll and zoom are unknown, and the page it displays cannot be chosen. Rendering the page
    ourselves makes every document type an `<img>`, so the overlay is uniform and "go to page
    12" becomes possible at all. `index` picks the file (a CI pair has two), `page` is 0-based
    within it — the same numbering `ProvenanceSpan.page_index` uses.

    An image file IS its own page and is served as-is. Rendering happens per request: caching
    is the integrator's call on real infrastructure, not something a proxy should presume.
    """
    job = _get_job_row(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    files = _spooled_document_files(job)
    if not files or index < 0 or index >= len(files):
        raise HTTPException(
            status_code=404, detail=f"no retained document for job {job_id}"
        )
    document_path = files[index]
    suffix = document_path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        if page != 0:
            raise HTTPException(
                status_code=404, detail=f"an image has one page, asked for {page}"
            )
        return FileResponse(document_path)
    if suffix != ".pdf":
        raise HTTPException(
            status_code=404, detail=f"cannot render a page of a {suffix} document"
        )
    import pymupdf

    with pymupdf.open(document_path) as document:
        if page < 0 or page >= document.page_count:
            raise HTTPException(
                status_code=404,
                detail=f"page {page} outside 0..{document.page_count - 1}",
            )
        pixmap = document[page].get_pixmap(dpi=PAGE_RENDER_DPI)
        return Response(content=pixmap.tobytes("png"), media_type="image/png")
