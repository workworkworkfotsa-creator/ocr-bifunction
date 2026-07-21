"""API maquette — a thin network door over the proven CI submission pipeline.

This is a PEDAGOGICAL MOCK, not production. Its only job is to let the contract run so we
can *see* what "having an API" means: upload one CI submission (any mix of images and/or a
combined recto+verso PDF), get back a stable envelope. The value lives in
`process_ci_submission`; this file only exposes it behind an HTTP door. The pipeline is NOT
touched.

The upload-facing contract has four outcomes (the upload UI acts on `status`):
  - validated   (200) — both sides received and confident;
  - pending     (202) — both sides received but doubtful: escalated async, poll job_id;
  - incomplete  (200) — one side missing: `missing` says which to ask the user for;
  - unrecognized(200) — not a CI submission at all.

Two lanes, one DOOR — the API never processes async work itself:
  - FAST PATH (in the request) — process_ci_submission with NO escalation engine; EVERY
    outcome leaves a D1 row (done/auto, needs_review, …) so D1 is the single source (④).
  - ESCALATION (off the request path) — a complete-but-doubtful (`review`) submission is
    SPOOLED to disk (`spool/<sub>/`, the row's `document_ref`) and written as a D1 row
    `status='received'`, answered `202 pending`. The row IS the queue entry: the SEPARATE
    watchdog worker process (worker_watchdog.py) claims it, re-runs WITH the VLM, and flips
    it to a terminal state. Restart-safe: the table survives, an in-memory queue would not.

The D1 `Repository` (SqliteRepository proxy) and the spool directory are the disposable
adapters of destination "domain 1 — jobs + queue": this API, the batch orchestrator and the
watchdog all write/read the SAME `ocr_jobs` table (repository.py) — one column contract,
several producers/consumers, one writer per phase. IT swaps store, hosting, auth, TLS.

Run it:
    uv run uvicorn api_maquette:app --reload          # the door
    uv run python worker_watchdog.py                  # the worker (separate process)
"""

from __future__ import annotations

import base64
import binascii
import os
import tempfile
import threading
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from ocr_bifunction.execution_policy import (
    DEFAULT_POLICY_CATEGORY,
    EXECUTION_LANE_FOR_ASYNC_MODE,
    EXECUTION_MODE_SYNC,
    EXECUTION_MODES,
    ExecutionPolicy,
    ExecutionPolicyRepository,
    SqliteExecutionPolicyRepository,
    resolve_execution,
)
from ocr_bifunction.capacity_settings import (
    CapacitySettings,
    CapacitySettingsRepository,
    DEFAULT_CAPACITY_SETTINGS,
    OVERFLOW_ACTION_REJECT_503,
    OVERFLOW_ACTIONS,
    SqliteCapacitySettingsRepository,
    load_capacity_settings,
)
from ocr_bifunction.conformity_policy import (
    CONFORMITY_ACTION_BLOCK_HOLDER,
    CONFORMITY_ACTIONS,
    ConformityPolicy,
    ConformityPolicyRepository,
    DEFAULT_CONFORMITY_CATEGORY,
    SqliteConformityPolicyRepository,
    resolve_conformity_action,
)
from ocr_bifunction.context_assembly import (
    ATTESTATION_REFERENCE_ROLES_KEY,
    REFERENCE_ROLE_FIELD_KEYS,
    collect_validated_attestations,
)
from ocr_bifunction.issuer_registry import (
    IssuerEntry,
    IssuerRegistryRepository,
    SqliteIssuerRegistryRepository,
)
from ocr_bifunction.intake import handle_document, job_from_outcome
from ocr_bifunction.orchestrator import BatchItem
from ocr_bifunction.promotion import promote_suggestion
from ocr_bifunction.reader import IMAGE_SUFFIXES, OcrEngine
from ocr_bifunction.store import Store
from ocr_bifunction.repository import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_RECEIVED,
    STATUS_REJECTED,
    Job,
    Repository,
    SqliteRepository,
)
from ocr_bifunction.review_repository import (
    SUGGESTION_PENDING,
    SUGGESTION_REJECTED,
    Review,
    ReviewRepository,
    SqliteReviewRepository,
)
from ocr_bifunction.template import ValidationContext, load_templates, payload_value
from ocr_bifunction.verdict import Verdict
from ocr_bifunction.template_repository import (
    SqliteTemplateRepository,
    TemplateRepository,
)
from ocr_bifunction.use_case_key import (
    KNOWN_USE_CASES,
    SqliteUseCaseKeyRepository,
    UseCaseKeyRepository,
    resolve_use_case,
)

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"
# Default suffix when an uploaded filename carries none (a CI photo is usually one format).
DEFAULT_SUFFIX = ".jpg"

# Where doubtful submissions' bytes wait for the watchdog (PII on disk, gitignored; the
# worker purges each job's directory on terminal state). Env-overridable like the store.
SPOOL_ROOT = Path(os.environ.get("OCR_SPOOL_PATH", "spool"))

app = FastAPI(
    title="OCR BiFunction — API maquette",
    version="1",
    description="Thin mock door over the CI submission pipeline. Not production.",
)

# --- D1 store: the SAME `ocr_jobs` table the batch regime writes (repository.py). ----
# The escalation lifecycle (received -> processing -> done|needs_review|failed) now lives in
# D1, so this API and the batch orchestrator exercise ONE column contract. A single shared
# connection is opened lazily and every access is guarded by `_repository_lock`: the worker
# thread writes `status`, request threads read/enqueue — one writer per phase, no race.
STORE_PATH = os.environ.get("OCR_STORE_PATH", "ocr_store.sqlite")

_repository: Repository | None = None
_repository_lock = threading.Lock()

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


# --- Admission control: the sync lane is CAPPED (worst-case doctrine, modest servers). --
# A counter under its own lock (not a Semaphore) so the LIMIT stays a live config value:
# ops raises SYNC_CONCURRENCY_LIMIT on day-J hardware without restarting the door.
_capacity_lock = threading.Lock()
_active_sync_count = 0


def _try_acquire_sync_slot(limit: int) -> bool:
    global _active_sync_count
    with _capacity_lock:
        if _active_sync_count >= limit:
            return False
        _active_sync_count += 1
        return True


def _release_sync_slot() -> None:
    global _active_sync_count
    with _capacity_lock:
        _active_sync_count -= 1


def _new_store() -> Store:
    """One file-backed Store connection for an API repo. check_same_thread=False lets the
    request threads and the async worker share it; the caller serializes with _repository_lock.
    One Store per repo (as before) keeps the current per-connection concurrency semantics —
    consolidating to a single shared connection is a separate, lock-audited change."""
    return Store(STORE_PATH, check_same_thread=False)


def _ensure_repository() -> Repository:
    """Build the shared D1 store once (lazy). Caller MUST hold `_repository_lock`."""
    global _repository
    if _repository is None:
        _repository = SqliteRepository(_new_store())
    return _repository


def _save_job(job: Job) -> int:
    with _repository_lock:
        return _ensure_repository().save(job)


def _get_job_row(job_id: int) -> Job | None:
    with _repository_lock:
        return _ensure_repository().get(job_id)


def _update_job(
    job_id: int,
    status: str,
    *,
    verdict: str | None = None,
    record_fields: dict[str, dict] | None = None,
    reasons: list[str] | None = None,
) -> None:
    with _repository_lock:
        _ensure_repository().update_status(
            job_id,
            status,
            verdict=verdict,
            record_fields=record_fields,
            reasons=reasons,
        )


# The fast OCR engine is heavy to build (loads ONNX models on CPU): built once, reused.
_engine: OcrEngine | None = None


def _get_engine() -> OcrEngine:
    global _engine
    if _engine is None:
        from ocr_bifunction.rapidocr_engine import RapidOcrEngine

        _engine = RapidOcrEngine()
    return _engine


# --- The contract, written black on white (this is what IT wants to see). ------------


class FileUpload(BaseModel):
    """One uploaded file. The server figures out which card side(s) it carries."""

    filename: str = Field(description="Original name, used for the file suffix.")
    content_base64: str = Field(description="File bytes, base64-encoded.")


class ValidateRequest(BaseModel):
    """One CI submission: any mix of images and/or a combined recto+verso PDF. The server
    extracts the card sides and decides whether the submission is complete."""

    files: list[FileUpload] = Field(
        description="The uploaded file(s) of one submission."
    )
    document_type: str | None = Field(
        default=None,
        description=(
            "Optional document-type hint (e.g. 'carte_identite') scoping template matching "
            "to that category. None tries every template."
        ),
    )
    request_id: str | None = Field(
        default=None,
        description="Idempotency key: replaying it returns the first result verbatim.",
    )
    processing_mode: Literal["sync", "async_immediate", "async_nightly"] | None = Field(
        default=None,
        description=(
            "Optional caller hint on WHEN to process. Honored only where the category's "
            "execution policy allows override; otherwise the policy wins and the ignored "
            "hint is traced in `reasons`."
        ),
    )
    expected_holder_name: str | None = Field(
        default=None,
        description=(
            "Optional DECLARED holder (manual entry for now — the dossier says whose "
            "document this is). Feeds the reconcile_ci anti-fraud check as the CI "
            "reference name; absent, that check routes to review (fail-loud). A future "
            "upgrade reads it from the validated CI record instead."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "files": [
                    {"filename": "ci_recto.jpg", "content_base64": "<base64>"},
                    {"filename": "ci_verso.jpg", "content_base64": "<base64>"},
                ],
                "document_type": "carte_identite",
                "request_id": "demo-1",
            }
        }
    }


class ValidateResponse(BaseModel):
    """Always this shape — never a mute response when something fails.

    `validated` (200): confident (auto). `pending` (202): a CI complete + doubtful, escalated
    async (poll job_id). `needs_review` (200): doubtful non-CI structured/non-structured doc,
    decided synchronously (no VLM escalation). `rejected` (200): PROVEN invalid (anti-fraud
    verdict — bad dates, invented code, holder≠CI), auto-terminal, no human. `incomplete`
    (200): a CI side is missing (`missing` says which). `unrecognized` (200): not a
    recognizable document.
    """

    status: Literal[
        "validated", "pending", "needs_review", "rejected", "incomplete", "unrecognized"
    ]
    verdict: Literal["auto", "review", "reject"] | None
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)  # subset of ["recto", "verso"]
    # The D1 row id (ocr_jobs.job_id). EVERY submission now leaves a row (single source,
    # stage ④), so this is set on every response — pollable for `pending`, a trace otherwise.
    job_id: int | None = None


class JobResponse(BaseModel):
    """The async follow-up. The worker advances a job to a terminal D1 state; `verdict`/`reasons`
    carry the final escalated outcome (which may still be `review`). The verso-read provenance —
    a CI-only diagnostic with no D1 column — is folded into `reasons` ('verso read via: …')."""

    status: Literal["pending", "done"]
    verdict: Literal["auto", "review", "reject"] | None = None
    reasons: list[str] = Field(default_factory=list)


def _http_status_for(response: ValidateResponse) -> int:
    """202 says 'received, I'm working' for the escalation lane; 200 for everything else."""
    return (
        status.HTTP_202_ACCEPTED if response.status == "pending" else status.HTTP_200_OK
    )


# --- Escalation lane: spool the bytes, write a `received` row — the watchdog does the rest.


def _write_files(files: list[tuple[str, bytes]], directory: Path) -> list[Path]:
    """Write (filename, bytes) uploads to a directory, preserving each file's suffix."""
    paths: list[Path] = []
    for index, (filename, data) in enumerate(files):
        suffix = Path(filename).suffix or DEFAULT_SUFFIX
        file_path = directory / f"file_{index}{suffix}"
        file_path.write_bytes(data)
        paths.append(file_path)
    return paths


def _spool_files(files: list[tuple[str, bytes]]) -> str:
    """Persist one submission's bytes to a fresh spool directory; return its path.

    The spool is the document's WAITING ROOM: async work reads it, and a `needs_review`
    row keeps it so the reviewer can SEE the document next to its extraction. The
    watchdog purges it at every terminal state except needs_review; the sweep purges it
    when the human decision closes the job (PII hygiene, one owner per phase)."""
    spool_directory = SPOOL_ROOT / f"sub_{uuid.uuid4().hex[:12]}"
    spool_directory.mkdir(parents=True, exist_ok=False)
    _write_files(files, spool_directory)
    return str(spool_directory)


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


@app.post("/v1/documents:validate", response_model=ValidateResponse)
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


@app.get("/v1/jobs/{job_id}", response_model=JobResponse)
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


def _spooled_document_files(job: Job) -> list[Path]:
    """The job's spooled files, [] when nothing is retained (purged or never spooled)."""
    if not job.document_ref:
        return []
    spool_directory = Path(job.document_ref)
    if not spool_directory.is_dir():
        return []
    return sorted(path for path in spool_directory.iterdir() if path.is_file())


@app.get("/v1/jobs/{job_id}/document")
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


# Display resolution for the rendered page preview. ANY value works: provenance spans are
# normalized to the page, so the overlay is dpi-independent — precisely what normalizing bought.
PAGE_RENDER_DPI = 150


@app.get("/v1/jobs/{job_id}/page")
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


# --- Local-test UI + review surface (disposable adapters, BRIEF-fonctionnement-mix). ---
# The pages are plain HTML files (ui/), skins over the endpoints below. Writer contract:
# these endpoints READ D1 and WRITE D3 (decision, suggestion validation) — never D1.status;
# the watchdog closes D1 on its sweep. Promotion (validate a suggestion) writes D2.

UI_DIRECTORY = PROJECT_ROOT / "ui"

_review_repository: ReviewRepository | None = None
_template_repository: TemplateRepository | None = None


def _ensure_review_repository() -> ReviewRepository:
    """Build the shared D3 store once (lazy). Caller MUST hold `_repository_lock`."""
    global _review_repository
    if _review_repository is None:
        _review_repository = SqliteReviewRepository(_new_store())
    return _review_repository


def _ensure_template_repository() -> TemplateRepository:
    """Build the shared D2 store once (lazy). Caller MUST hold `_repository_lock`.

    The committed `templates/*.json` are re-seeded on first use (idempotent upsert):
    the files stay the anonymized SEED, D2 is the runtime source the router reads."""
    global _template_repository
    if _template_repository is None:
        _template_repository = SqliteTemplateRepository(_new_store())
        _template_repository.seed_from_directory(TEMPLATES_DIRECTORY)
    return _template_repository


_execution_policy_repository: ExecutionPolicyRepository | None = None
_issuer_registry_repository: IssuerRegistryRepository | None = None
_conformity_policy_repository: ConformityPolicyRepository | None = None
_capacity_settings_repository: CapacitySettingsRepository | None = None
_use_case_key_repository: UseCaseKeyRepository | None = None


def _ensure_capacity_settings_repository() -> CapacitySettingsRepository:
    """Build the shared capacity-levers store once (lazy). Caller MUST hold `_repository_lock`."""
    global _capacity_settings_repository
    if _capacity_settings_repository is None:
        _capacity_settings_repository = SqliteCapacitySettingsRepository(_new_store())
        _capacity_settings_repository.seed_defaults()
    return _capacity_settings_repository


def _load_capacity_settings() -> CapacitySettings:
    """The admission levers, read fresh per request — ops edits apply immediately."""
    with _repository_lock:
        return load_capacity_settings(_ensure_capacity_settings_repository())


def _ensure_conformity_policy_repository() -> ConformityPolicyRepository:
    """Build the shared conformity-policy store once (lazy). Caller MUST hold `_repository_lock`."""
    global _conformity_policy_repository
    if _conformity_policy_repository is None:
        _conformity_policy_repository = SqliteConformityPolicyRepository(_new_store())
        _conformity_policy_repository.seed_defaults()
    return _conformity_policy_repository


def _ensure_issuer_registry_repository() -> IssuerRegistryRepository:
    """Build the shared issuer-registry store once (lazy). Caller MUST hold `_repository_lock`."""
    global _issuer_registry_repository
    if _issuer_registry_repository is None:
        _issuer_registry_repository = SqliteIssuerRegistryRepository(_new_store())
    return _issuer_registry_repository


def _ensure_use_case_key_repository() -> UseCaseKeyRepository:
    """Build the shared use-case-key store once (lazy). Caller MUST hold `_repository_lock`.

    No seed_defaults: keys are secrets, issued explicitly through `/use-case-keys`, never
    conjured from an in-code default (unlike the other leviers surfaces)."""
    global _use_case_key_repository
    if _use_case_key_repository is None:
        _use_case_key_repository = SqliteUseCaseKeyRepository(_new_store())
    return _use_case_key_repository


def _ensure_execution_policy_repository() -> ExecutionPolicyRepository:
    """Build the shared execution-policy store once (lazy). Caller MUST hold `_repository_lock`.

    In-code defaults are seeded on first use (MISSING rows only): an operator edit made
    through /policies survives restarts, the doctrine of the config surface."""
    global _execution_policy_repository
    if _execution_policy_repository is None:
        _execution_policy_repository = SqliteExecutionPolicyRepository(_new_store())
        _execution_policy_repository.seed_defaults()
    return _execution_policy_repository


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def upload_page() -> str:
    return (UI_DIRECTORY / "upload.html").read_text(encoding="utf-8")


@app.get("/policies", response_class=HTMLResponse, include_in_schema=False)
def policies_page() -> str:
    return (UI_DIRECTORY / "policies.html").read_text(encoding="utf-8")


@app.get("/registry", response_class=HTMLResponse, include_in_schema=False)
def registry_page() -> str:
    return (UI_DIRECTORY / "registry.html").read_text(encoding="utf-8")


@app.get("/use-case-keys", response_class=HTMLResponse, include_in_schema=False)
def use_case_keys_page() -> str:
    return (UI_DIRECTORY / "use_case_keys.html").read_text(encoding="utf-8")


@app.get("/review", response_class=HTMLResponse, include_in_schema=False)
def review_page() -> str:
    return (UI_DIRECTORY / "review.html").read_text(encoding="utf-8")


@app.get("/v1/document-types")
def document_types() -> dict:
    """The upload select box options — DERIVED from the ACTIVE D2 templates, never
    hardcoded (an organically promoted category appears here on its own)."""
    with _repository_lock:
        active_templates = _ensure_template_repository().active_templates()
    categories = sorted(
        {
            template.get("category")
            for template in active_templates
            if template.get("category")
        }
    )
    return {"document_types": categories}


@app.get("/v1/reviews/queue")
def review_queue() -> dict:
    """The D1 needs_review rows + each one's D3 decision state (what the human sees)."""
    with _repository_lock:
        jobs = _ensure_repository().pending(STATUS_NEEDS_REVIEW)
        review_repository = _ensure_review_repository()
        payload = []
        for job in jobs:
            review = review_repository.by_job(job.job_id)
            document_files = _spooled_document_files(job)
            payload.append(
                {
                    "job_id": job.job_id,
                    "source": job.source,
                    "category_lane": job.category_lane,
                    "category": job.category,
                    "template_id": job.template_id,
                    "record_fields": job.record_fields,
                    "reasons": job.reasons,
                    "decision": review.decision if review else None,
                    # The retained document(s), so the reviewer sees the doc NEXT TO the
                    # extraction (and can judge the extraction itself, not just the record).
                    "documents": [
                        {
                            "url": f"/v1/jobs/{job.job_id}/document?index={file_index}",
                            "filename": file_path.name,
                            "suffix": file_path.suffix.lower(),
                        }
                        for file_index, file_path in enumerate(document_files)
                    ],
                }
            )
    return {"jobs": payload}


class DecisionRequest(BaseModel):
    """The human's verdict on a reviewed record — written to D3, never to D1."""

    decision: Literal["accept", "reject"]
    comment: str | None = None


class FieldCorrectionRequest(BaseModel):
    """The reviewer's edited values, `{field_name: value}` — only the fields they touched."""

    fields: dict[str, str | None]


@app.post("/v1/reviews/{job_id}/fields")
def record_field_corrections(job_id: int, request: FieldCorrectionRequest) -> dict:
    """Store the human's corrections of an extracted record in D3 (never in D1).

    Seeing the zone lets a reviewer JUDGE a value; correcting it means writing one, and the
    writer rule decides where: the UI writes D3, the watchdog writes D1. So the edit is staged
    here as `{field: {"from": what the machine read, "to": what the human put}}` — keeping both
    is what makes the correction auditable later, and what tells a recurring OCR weakness from
    a one-off. The watchdog APPLIES it to D1 when the review is accepted; until then D1 still
    says, honestly, what the machine read.

    A value equal to the machine's is NOT a correction and is dropped, so an untouched form
    submits nothing. Unknown field names are refused rather than silently invented.
    """
    with _repository_lock:
        job = _ensure_repository().get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
        unknown = sorted(set(request.fields) - set(job.record_fields))
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"not fields of this record: {', '.join(unknown)}",
            )
        corrections = {
            name: {"from": payload_value(job.record_fields, name), "to": value}
            for name, value in request.fields.items()
            if value != payload_value(job.record_fields, name)
        }
        review_repository = _ensure_review_repository()
        review = review_repository.by_job(job_id)
        if review is None:
            review_id = review_repository.open_review(
                Review(
                    job_id=job_id,
                    projection={
                        "source": job.source,
                        "lane": job.category_lane,
                        "verdict": job.verdict,
                    },
                )
            )
        else:
            review_id = review.review_id
        review_repository.record_field_corrections(review_id, corrections)
    return {
        "review_id": review_id,
        "corrected": sorted(corrections),
        "note": "applied to the D1 record by the watchdog when the review is accepted",
    }


@app.post("/v1/reviews/{job_id}/decision")
def record_review_decision(job_id: int, request: DecisionRequest) -> dict:
    """Record the human decision in D3 (opening the review row if none exists yet). The D1
    job stays needs_review here — the WATCHDOG closes it on its next sweep (one writer)."""
    with _repository_lock:
        job = _ensure_repository().get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
        review_repository = _ensure_review_repository()
        review = review_repository.by_job(job_id)
        if review is None:
            review_id = review_repository.open_review(
                Review(
                    job_id=job_id,
                    projection={
                        "source": job.source,
                        "lane": job.category_lane,
                        "verdict": job.verdict,
                    },
                )
            )
        else:
            review_id = review.review_id
        review_repository.record_decision(
            review_id, comment=request.comment, decision=request.decision
        )
    return {
        "review_id": review_id,
        "decision": request.decision,
        "note": "D1 job will be closed by the watchdog's next sweep",
    }


@app.get("/v1/suggestions/pending")
def pending_suggestions() -> dict:
    """The D3 pending template suggestions. Closed-list suggestions (SLM lane) show the
    known template's validation criteria READ-ONLY; a DRAFT suggestion (drafting lane)
    carries its FULL template, whose candidate checks the reviewer TICKS at validation
    (compute-all/config-requires: the ticking writes the required block)."""
    with _repository_lock:
        pending = _ensure_review_repository().pending_suggestions()
    templates_by_id = {
        template["template_id"]: template
        for template in load_templates(TEMPLATES_DIRECTORY)
    }
    payload = []
    for review in pending:
        suggestion = review.suggestion
        draft_template = suggestion.template
        if draft_template is not None:
            validation = draft_template.get("validation", {})
        else:
            validation = templates_by_id.get(suggestion.template_id or "", {}).get(
                "validation", {}
            )
        payload.append(
            {
                "review_id": review.review_id,
                "job_id": review.job_id,
                "template_id": suggestion.template_id,
                "anchors": suggestion.anchors,
                "validation": validation,
                "draft_template": draft_template,
            }
        )
    return {"suggestions": payload}


class ValidateSuggestionRequest(BaseModel):
    """The reviewer's ticking on a DRAFT suggestion: the subset of the draft's candidate
    checks that become REQUIRED, plus (optionally) the attestation reference ROLES — the
    métier's mapping of which draft fields play holder / issue / expiry when documents
    of this template corroborate a titre (corroborated_by). The human selects among the
    draft's own fields, never authors rules here (curation surface). Absent body =
    every candidate stays required, no roles declared."""

    required: list[dict] | None = None
    reference_roles: dict[str, str] | None = None


@app.post("/v1/suggestions/{review_id}/validate")
def validate_suggestion(
    review_id: int, request: ValidateSuggestionRequest | None = None
) -> dict:
    """Promote a pending suggestion: the curated template becomes ACTIVE in D2 and the D3
    suggestion flips validated (promotion.py, the third writer of the column contract).

    A DRAFT suggestion is promoted with the reviewer's TICKED checks as its `required`
    block (each must be one of the draft's candidates); a closed-list suggestion keeps
    the known template's content unchanged."""
    with _repository_lock:
        review = _ensure_review_repository().get(review_id)
        if review is None or review.suggestion is None:
            raise HTTPException(
                status_code=404, detail=f"no suggestion on review {review_id}"
            )
        if review.suggestion.status != SUGGESTION_PENDING:
            raise HTTPException(
                status_code=409,
                detail=f"suggestion already {review.suggestion.status}",
            )
        draft_template = review.suggestion.template
        if draft_template is not None:
            curated_template = draft_template
            if request is not None and request.required is not None:
                candidates = draft_template.get("validation", {}).get("required", [])
                # The reviewer may ATTACH a severity to a ticked candidate (the métier
                # hardens/softens a check) — comparison against the draft's candidates
                # ignores that key; the value itself is guarded.
                rejected_rules = [
                    rule
                    for rule in request.required
                    if {key: value for key, value in rule.items() if key != "severity"}
                    not in candidates
                ]
                if rejected_rules:
                    raise HTTPException(
                        status_code=400,
                        detail=f"not draft candidates: {rejected_rules}",
                    )
                invalid_severities = [
                    rule["severity"]
                    for rule in request.required
                    if "severity" in rule
                    and rule["severity"] not in ("reject", "review")
                ]
                if invalid_severities:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"unknown severity values: {invalid_severities} "
                            "(expected 'reject' or 'review')"
                        ),
                    )
                curated_template = {
                    **draft_template,
                    "validation": {
                        "comment": (
                            "human-curated at review: required checks ticked "
                            "among the draft's candidates"
                        ),
                        "required": request.required,
                    },
                }
            if request is not None and request.reference_roles is not None:
                # The métier's roles assignment: all three roles, each naming one of
                # the draft's OWN fields (the human maps, never invents a field).
                roles = request.reference_roles
                if sorted(roles) != sorted(REFERENCE_ROLE_FIELD_KEYS):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "reference_roles must name exactly "
                            f"{list(REFERENCE_ROLE_FIELD_KEYS)}"
                        ),
                    )
                draft_field_names = {
                    field_entry["name"]
                    for field_entry in draft_template.get("fields", [])
                }
                unknown_fields = [
                    field_name
                    for field_name in roles.values()
                    if field_name not in draft_field_names
                ]
                if unknown_fields:
                    raise HTTPException(
                        status_code=400,
                        detail=f"not draft fields: {unknown_fields}",
                    )
                curated_template = {
                    **curated_template,
                    ATTESTATION_REFERENCE_ROLES_KEY: roles,
                }
        else:
            templates_by_id = {
                template["template_id"]: template
                for template in load_templates(TEMPLATES_DIRECTORY)
            }
            curated_template = templates_by_id.get(review.suggestion.template_id or "")
            if curated_template is None:
                raise HTTPException(
                    status_code=409,
                    detail="curated template not found — v1 promotes known template ids only",
                )
        promoted_template_id = promote_suggestion(
            review_id,
            curated_template,
            template_repository=_ensure_template_repository(),
            review_repository=_ensure_review_repository(),
        )
    return {"promoted_template_id": promoted_template_id, "active": True}


@app.post("/v1/suggestions/{review_id}/reject")
def reject_suggestion(review_id: int) -> dict:
    """Reject a pending suggestion (the model was wrong, or the human declines) — D3 only."""
    with _repository_lock:
        review = _ensure_review_repository().get(review_id)
        if review is None or review.suggestion is None:
            raise HTTPException(
                status_code=404, detail=f"no suggestion on review {review_id}"
            )
        _ensure_review_repository().set_suggestion_status(
            review_id, SUGGESTION_REJECTED
        )
    return {"review_id": review_id, "suggestion_status": SUGGESTION_REJECTED}


# --- Execution-policy surface: the /policies page's endpoints (reads the door obeys). ---


def _policy_payload(policy: ExecutionPolicy) -> dict:
    return {
        "category": policy.category,
        "execution_mode": policy.execution_mode,
        "override_allowed": policy.override_allowed,
        "updated_at": policy.updated_at,
    }


@app.get("/v1/execution-policies")
def execution_policies() -> dict:
    """Every policy row + the categories known to D2 (rows the operator may still add).

    The door resolves each upload against these rows; a category without its own row
    falls back to the '*' default. `execution_modes` feeds the page's select box."""
    with _repository_lock:
        policies = _ensure_execution_policy_repository().all_policies()
        active_templates = _ensure_template_repository().active_templates()
    categories_with_policy = {policy.category for policy in policies}
    known_categories = sorted(
        {
            template.get("category")
            for template in active_templates
            if template.get("category")
        }
        - categories_with_policy
    )
    return {
        "policies": [_policy_payload(policy) for policy in policies],
        "categories_without_policy": known_categories,
        "execution_modes": list(EXECUTION_MODES),
    }


class ExecutionPolicyRequest(BaseModel):
    """One policy row as the /policies page writes it."""

    execution_mode: Literal["sync", "async_immediate", "async_nightly"]
    override_allowed: bool = False


@app.put("/v1/execution-policies/{category}")
def put_execution_policy(category: str, request: ExecutionPolicyRequest) -> dict:
    """Create or update the policy row for a category ('*' = the default row). Takes
    effect on the NEXT upload — no restart, no redeploy (the door re-reads per request)."""
    policy = ExecutionPolicy(
        category=category,
        execution_mode=request.execution_mode,
        override_allowed=request.override_allowed,
    )
    with _repository_lock:
        _ensure_execution_policy_repository().upsert(policy)
        saved = _ensure_execution_policy_repository().get(category)
    return _policy_payload(saved) if saved else {"category": category}


@app.delete("/v1/execution-policies/{category}")
def delete_execution_policy(category: str) -> dict:
    """Remove a category's row so it falls back to the '*' default. The default row
    itself cannot be deleted — the door always needs a fallback."""
    if category == DEFAULT_POLICY_CATEGORY:
        raise HTTPException(
            status_code=400, detail="the '*' default policy cannot be deleted"
        )
    with _repository_lock:
        removed = _ensure_execution_policy_repository().delete(category)
    if not removed:
        raise HTTPException(
            status_code=404, detail=f"no execution policy for category {category!r}"
        )
    return {"category": category, "deleted": True}


# --- Issuer-registry surface: the métier list the issuer_registry check reads (D-e). ---


@app.get("/v1/issuer-registry")
def issuer_registry() -> dict:
    """The curated organisms. Empty registry = the issuer_registry check fails loud to
    review (an absent registry never proves an issuer legitimate)."""
    with _repository_lock:
        entries = _ensure_issuer_registry_repository().all_entries()
    return {
        "issuers": [
            {
                "identifier": entry.identifier,
                "label": entry.label,
                "updated_at": entry.updated_at,
            }
            for entry in entries
        ]
    }


class IssuerEntryRequest(BaseModel):
    """One organism as the registry page writes it (identifier lives in the path)."""

    label: str | None = None


@app.put("/v1/issuer-registry/{identifier}")
def put_issuer_entry(identifier: str, request: IssuerEntryRequest) -> dict:
    """Add or relabel a recognized organism (SIRET preferred as identifier). Takes
    effect on the NEXT validation — the check reads the registry per request."""
    entry = IssuerEntry(identifier=identifier, label=request.label)
    with _repository_lock:
        _ensure_issuer_registry_repository().upsert(entry)
    return {"identifier": identifier, "label": request.label}


@app.delete("/v1/issuer-registry/{identifier}")
def delete_issuer_entry(identifier: str) -> dict:
    """Remove an organism from the registry."""
    with _repository_lock:
        removed = _ensure_issuer_registry_repository().delete(identifier)
    if not removed:
        raise HTTPException(
            status_code=404, detail=f"unknown issuer identifier: {identifier!r}"
        )
    return {"identifier": identifier, "deleted": True}


# --- Use-case keys: the door's auth + consumer-profile resolution (D7). ---------------


@app.get("/v1/use-case-keys")
def use_case_keys() -> dict:
    """Every issued key (hash + use_case, never the raw secret — it was never stored)."""
    with _repository_lock:
        keys = _ensure_use_case_key_repository().all_keys()
    return {
        "keys": [
            {
                "key_id": key.key_id,
                "label": key.label,
                "use_case": key.use_case,
                "created_at": key.created_at,
            }
            for key in keys
        ],
        "known_use_cases": list(KNOWN_USE_CASES),
    }


class UseCaseKeyRequest(BaseModel):
    label: str
    use_case: str


@app.post("/v1/use-case-keys")
def create_use_case_key(request: UseCaseKeyRequest) -> dict:
    """Issue a new key. The raw secret is returned HERE ONLY — it is never stored, so it
    can never be shown again; a lost key must be revoked and a new one issued."""
    if request.use_case not in KNOWN_USE_CASES:
        raise HTTPException(
            status_code=422, detail=f"unknown use_case: {request.use_case!r}"
        )
    with _repository_lock:
        issued = _ensure_use_case_key_repository().create(
            request.label, request.use_case
        )
    return {
        "key_id": issued.key_id,
        "label": issued.label,
        "use_case": issued.use_case,
        "api_key": issued.raw_key,
    }


@app.delete("/v1/use-case-keys/{key_id}")
def revoke_use_case_key(key_id: int) -> dict:
    """Revoke a key. Past D1 rows already carry a snapshot of the use_case it resolved
    to — revoking never rewrites history, it only stops FUTURE requests using it."""
    with _repository_lock:
        removed = _ensure_use_case_key_repository().revoke(key_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"unknown key_id: {key_id}")
    return {"key_id": key_id, "revoked": True}


# --- Conformity-policy surface: WHAT a proven non-conformity does (métier config). ----


def _conformity_policy_payload(policy: ConformityPolicy) -> dict:
    return {
        "category": policy.category,
        "action": policy.action,
        "updated_at": policy.updated_at,
    }


@app.get("/v1/conformity-policies")
def conformity_policies() -> dict:
    """Every conformity-policy row + the action vocabulary for the page's select."""
    with _repository_lock:
        policies = _ensure_conformity_policy_repository().all_policies()
    return {
        "policies": [_conformity_policy_payload(policy) for policy in policies],
        "conformity_actions": list(CONFORMITY_ACTIONS),
    }


class ConformityPolicyRequest(BaseModel):
    """One conformity-policy row as the /policies page writes it."""

    action: Literal["block", "block_holder", "flag_and_continue"]


@app.put("/v1/conformity-policies/{category}")
def put_conformity_policy(category: str, request: ConformityPolicyRequest) -> dict:
    """Create or update the reaction for a category ('*' = the default row). Takes
    effect on the NEXT upload — the door resolves per request."""
    policy = ConformityPolicy(category=category, action=request.action)
    with _repository_lock:
        _ensure_conformity_policy_repository().upsert(policy)
        saved = _ensure_conformity_policy_repository().get(category)
    return _conformity_policy_payload(saved) if saved else {"category": category}


@app.delete("/v1/conformity-policies/{category}")
def delete_conformity_policy(category: str) -> dict:
    """Remove a category's row so it falls back to '*'. The default row is undeletable."""
    if category == DEFAULT_CONFORMITY_CATEGORY:
        raise HTTPException(
            status_code=400,
            detail="the '*' default conformity policy cannot be deleted",
        )
    with _repository_lock:
        removed = _ensure_conformity_policy_repository().delete(category)
    if not removed:
        raise HTTPException(
            status_code=404, detail=f"no conformity policy for category {category!r}"
        )
    return {"category": category, "deleted": True}


# --- Capacity levers: the door's admission knobs (infra config, hardware day-J). ------


@app.get("/v1/capacity-settings")
def capacity_settings() -> dict:
    """The admission levers as currently resolved (defaults folded in) + the raw store."""
    with _repository_lock:
        repository = _ensure_capacity_settings_repository()
        resolved = load_capacity_settings(repository)
        stored = repository.all_settings()
    return {
        "sync_concurrency_limit": resolved.sync_concurrency_limit,
        "sync_overflow_action": resolved.sync_overflow_action,
        "overflow_actions": list(OVERFLOW_ACTIONS),
        "stored": stored,
        "defaults": DEFAULT_CAPACITY_SETTINGS,
    }


class CapacitySettingsRequest(BaseModel):
    """The two admission levers as the /policies page writes them."""

    sync_concurrency_limit: int = Field(ge=1, le=64)
    sync_overflow_action: Literal["defer", "reject_503"]


@app.put("/v1/capacity-settings")
def put_capacity_settings(request: CapacitySettingsRequest) -> dict:
    """Update the admission levers — takes effect on the NEXT upload (no restart):
    raise the cap on day-J hardware, or switch the overflow behavior."""
    with _repository_lock:
        repository = _ensure_capacity_settings_repository()
        repository.upsert("SYNC_CONCURRENCY_LIMIT", str(request.sync_concurrency_limit))
        repository.upsert("SYNC_OVERFLOW_ACTION", request.sync_overflow_action)
    return {
        "sync_concurrency_limit": request.sync_concurrency_limit,
        "sync_overflow_action": request.sync_overflow_action,
    }


@app.get("/v1/reviews/nonconformities")
def nonconformity_queue() -> dict:
    """The proven non-conforme documents (D1 `rejected`) awaiting the human review /
    compliance handoff: evidence (template, computed checks, reasons) + retained bytes.
    A decision on the row (« clore ») lets the watchdog purge its spool."""
    with _repository_lock:
        jobs = _ensure_repository().pending(STATUS_REJECTED)
        review_repository = _ensure_review_repository()
        payload = []
        for job in jobs:
            review = review_repository.by_job(job.job_id)
            document_files = _spooled_document_files(job)
            payload.append(
                {
                    "job_id": job.job_id,
                    "source": job.source,
                    "category": job.category,
                    "template_id": job.template_id,
                    "record_fields": job.record_fields,
                    "reasons": job.reasons,
                    "expected_holder_name": job.expected_holder_name,
                    "decision": review.decision if review else None,
                    "documents": [
                        {
                            "url": f"/v1/jobs/{job.job_id}/document?index={file_index}",
                            "filename": file_path.name,
                            "suffix": file_path.suffix.lower(),
                        }
                        for file_index, file_path in enumerate(document_files)
                    ],
                }
            )
    return {"jobs": payload}
