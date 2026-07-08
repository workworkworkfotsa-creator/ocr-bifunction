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
  - ESCALATION (off the request path) — a complete-but-doubtful (`human`) submission is
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
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse
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
from ocr_bifunction.pipeline import CiSubmissionResult, process_ci_submission
from ocr_bifunction.promotion import promote_suggestion
from ocr_bifunction.reader import OcrEngine
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
from ocr_bifunction.router import RoutedDocument, route_document
from ocr_bifunction.template import load_templates
from ocr_bifunction.template_repository import (
    SqliteTemplateRepository,
    TemplateRepository,
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
_idempotency_cache: dict[str, "ValidateResponse"] = {}


def _ensure_repository() -> Repository:
    """Build the shared D1 store once (lazy). Caller MUST hold `_repository_lock`."""
    global _repository
    if _repository is None:
        _repository = SqliteRepository(STORE_PATH, check_same_thread=False)
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
    record_fields: dict[str, str | None] | None = None,
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
    verdict: Literal["auto", "human", "reject"] | None
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)  # subset of ["recto", "verso"]
    # The D1 row id (ocr_jobs.job_id). EVERY submission now leaves a row (single source,
    # stage ④), so this is set on every response — pollable for `pending`, a trace otherwise.
    job_id: int | None = None


class JobResponse(BaseModel):
    """The async follow-up. The worker advances a job to a terminal D1 state; `verdict`/`reasons`
    carry the final escalated outcome (which may still be `human`). The verso-read provenance —
    a CI-only diagnostic with no D1 column — is folded into `reasons` ('verso read via: …')."""

    status: Literal["pending", "done"]
    verdict: Literal["auto", "human", "reject"] | None = None
    reasons: list[str] = Field(default_factory=list)


def _http_status_for(response: ValidateResponse) -> int:
    """202 says 'received, I'm working' for the escalation lane; 200 for everything else."""
    return (
        status.HTTP_202_ACCEPTED if response.status == "pending" else status.HTTP_200_OK
    )


# --- Mapping: a CiSubmissionResult -> the wire contract. ------------------------------


def _map_complete_auto(result: CiSubmissionResult) -> ValidateResponse:
    """A complete + confident submission. Only `auto` reaches here synchronously; the
    `human` (doubtful) case is routed to the escalation lane by the endpoint."""
    return ValidateResponse(
        status="validated", verdict="auto", reasons=result.record.reasons
    )


def _map_incomplete_or_unrecognized(result: CiSubmissionResult) -> ValidateResponse:
    """A submission with a missing side, or not a CI at all — no async work, the UI acts."""
    wire_status = "incomplete" if result.status == "incomplete" else "unrecognized"
    return ValidateResponse(
        status=wire_status, verdict=None, reasons=result.reasons, missing=result.missing
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


def _spool_and_enqueue(
    files: list[tuple[str, bytes]],
    category: str | None,
    fast_reasons: list[str],
    request_id: str | None,
    *,
    category_lane: str = "ci",
    execution_lane: str = "escalation",
) -> int:
    """Persist the uploaded bytes to the spool and insert a `received` D1 row pointing at them.

    That row IS the queue entry (the status column is the signal): the watchdog worker — a
    separate process — claims it, processes the spooled files (`document_ref`), writes the
    terminal state and purges the spool directory (PII hygiene). Two producers use this door:
    the CI doubt escalation (defaults) and the execution policy's async modes
    (category_lane 'unrouted', execution_lane 'deferred'/'nightly')."""
    spool_directory = SPOOL_ROOT / f"sub_{uuid.uuid4().hex[:12]}"
    spool_directory.mkdir(parents=True, exist_ok=False)
    _write_files(files, spool_directory)
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
            document_ref=str(spool_directory),
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


def _run_fast_submission(
    files: list[tuple[str, bytes]], category: str | None
) -> CiSubmissionResult:
    """Run process_ci_submission on temp files with NO escalation engine (the fast path).

    Temp files live under the system temp dir and the directory is removed on exit, so the
    PII does not linger. Raises HTTPException(500) on a pipeline/engine crash.
    """
    with tempfile.TemporaryDirectory(prefix="ocr_bifunction_api_") as temp_directory:
        paths = _write_files(files, Path(temp_directory))
        try:
            return process_ci_submission(
                paths, _get_engine(), TEMPLATES_DIRECTORY, category=category
            )
        except Exception as error:  # surface a pipeline/engine crash as 5xx, don't hide
            raise HTTPException(
                status_code=500, detail=f"pipeline failure: {type(error).__name__}"
            )


# --- Dispatch by document_type: route the upload to the flow it declares. -------------


def _run_route_document(
    files: list[tuple[str, bytes]], category: str | None
) -> RoutedDocument:
    """Route the first uploaded file through the 2-lane router (non-CI document types).

    Templates are read from D2 (`ocr_templates`, seeded from the committed JSON files),
    NOT from the files directly — so a template promoted through the review page matches
    on the very next upload (the growth loop closes through the API too). The CI flow
    keeps the directory (its templates are outside the suggestion loop, documented)."""
    filename, data = files[0]
    suffix = Path(filename).suffix or DEFAULT_SUFFIX
    with _repository_lock:
        active_templates = _ensure_template_repository().active_templates()
    with tempfile.TemporaryDirectory(prefix="ocr_bifunction_doc_") as temp_directory:
        file_path = Path(temp_directory) / f"file{suffix}"
        file_path.write_bytes(data)
        try:
            return route_document(
                file_path,
                TEMPLATES_DIRECTORY,
                _get_engine(),
                category=category,
                templates=active_templates,
            )
        except Exception as error:  # surface a pipeline/engine crash as 5xx, don't hide
            raise HTTPException(
                status_code=500, detail=f"pipeline failure: {type(error).__name__}"
            )


def _handle_ci_submission(
    files: list[tuple[str, bytes]], document_type: str | None, request_id: str | None
) -> ValidateResponse:
    """The CI lane: complete -> validated / pending(spool for the watchdog); else
    incomplete / unrecognized. EVERY outcome leaves a D1 row (single source, stage ④)."""
    result = _run_fast_submission(files, document_type)
    source = ", ".join(filename for filename, _ in files)
    if result.status == "complete" and result.record.verdict == "auto":
        record = result.record
        job_id = _save_job(
            Job(
                source=source,
                category_lane="ci",
                status=STATUS_DONE,
                execution_lane="fast",
                verdict="auto",
                category=document_type,
                template_id=record.template_id,
                record_fields=record.fields,
                reasons=record.reasons,
                request_id=request_id,
            )
        )
        wire = _map_complete_auto(result)
        wire.job_id = job_id
        return wire
    if result.status == "complete" and result.record.verdict == "reject":
        # A recto/verso identity mismatch: proven invalid, auto-terminal. No escalation —
        # a heavier OCR pass cannot rescue two sides that name different people.
        record = result.record
        job_id = _save_job(
            Job(
                source=source,
                category_lane="ci",
                status=STATUS_REJECTED,
                execution_lane="fast",
                verdict="reject",
                category=document_type,
                template_id=record.template_id,
                record_fields=record.fields,
                reasons=record.reasons,
                request_id=request_id,
            )
        )
        return ValidateResponse(
            status="rejected",
            verdict="reject",
            reasons=record.reasons,
            job_id=job_id,
        )
    if result.status == "complete":  # doubtful -> spool, the watchdog escalates
        job_id = _spool_and_enqueue(
            files, document_type, result.record.reasons, request_id
        )
        return ValidateResponse(
            status="pending", verdict=None, reasons=result.record.reasons, job_id=job_id
        )
    # incomplete / unrecognized: terminal, no async or human work pends on OUR side (the
    # uploader must act) -> persisted `done` with the story in reasons, verdict None.
    trace_reasons = list(result.reasons)
    if result.missing:
        trace_reasons.append(f"missing side(s): {', '.join(result.missing)}")
    job_id = _save_job(
        Job(
            source=source,
            category_lane="ci",
            status=STATUS_DONE,
            execution_lane="fast",
            category=document_type,
            reasons=trace_reasons,
            request_id=request_id,
        )
    )
    wire = _map_incomplete_or_unrecognized(result)
    wire.job_id = job_id
    return wire


# Structured router verdict -> D1 status and the upload-facing wire status. reject is the
# anti-fraud verdict: proven invalid, auto-terminal, no human review.
_D1_STATUS_FOR_VERDICT = {
    "auto": STATUS_DONE,
    "human": STATUS_NEEDS_REVIEW,
    "reject": STATUS_REJECTED,
}
_WIRE_STATUS_FOR_VERDICT = {
    "auto": "validated",
    "human": "needs_review",
    "reject": "rejected",
}


def _handle_single_document(
    files: list[tuple[str, bytes]], document_type: str | None, request_id: str | None
) -> ValidateResponse:
    """Non-CI document types: validate ONE doc via the 2-lane router (no VLM escalation).

    Structured verdict -> outcome: auto -> validated; human -> needs_review; reject ->
    rejected (proven invalid, anti-fraud verdict, terminal). Non-structured (RAG lane) ->
    needs_review (a human handles it). Multiple files: only the first is used. EVERY outcome
    leaves a D1 row; the sync needs_review rows are what the review UI lists.
    """
    routed = _run_route_document(files, document_type)
    source = files[0][0]  # the original filename (routed.source is the temp name)
    if routed.lane == "structured":
        d1_status = _D1_STATUS_FOR_VERDICT.get(
            routed.verdict or "", STATUS_NEEDS_REVIEW
        )
        wire_status = _WIRE_STATUS_FOR_VERDICT.get(routed.verdict or "", "needs_review")
        job_id = _save_job(
            Job(
                source=source,
                category_lane="structured",
                status=d1_status,
                execution_lane="fast",
                verdict=routed.verdict,
                category=routed.category,
                template_id=routed.template_id,
                record_fields=routed.fields,
                reasons=routed.reasons,
                request_id=request_id,
            )
        )
        return ValidateResponse(
            status=wire_status,  # type: ignore[arg-type]
            verdict=routed.verdict,  # type: ignore[arg-type]
            reasons=routed.reasons,
            job_id=job_id,
        )
    reasons = ["non-structured document — routed to retrieval / human review"]
    if routed.summary is not None and routed.summary.keywords:
        reasons.append("keywords: " + ", ".join(routed.summary.keywords))
    job_id = _save_job(
        Job(
            source=source,
            category_lane="rag",
            status=STATUS_NEEDS_REVIEW,
            execution_lane="fast",
            category=routed.category,
            reasons=reasons,
            request_id=request_id,
        )
    )
    return ValidateResponse(
        status="needs_review", verdict=None, reasons=reasons, job_id=job_id
    )


# --- Endpoints ------------------------------------------------------------------------


@app.post("/v1/documents:validate", response_model=ValidateResponse)
def validate_document(request: ValidateRequest, response: Response) -> ValidateResponse:
    """Validate one upload: the EXECUTION POLICY decides WHEN, `document_type` decides WHICH flow.

    First the execution policy for the category (fallback '*') resolves sync vs async,
    honoring the caller's optional `processing_mode` hint only where the policy allows
    override. Async -> spool + a `received` D1 row in the mode's lane ('deferred' drained
    continuously, 'nightly' drained by the night pass), answered 202 pending. Sync ->
    `carte_identite` runs the CI submission flow; any other type (facture, …) or none runs
    a single document through the 2-lane router.
    """
    if request.request_id and request.request_id in _idempotency_cache:
        cached = _idempotency_cache[request.request_id]
        response.status_code = _http_status_for(cached)
        return cached

    files = _decode_files(request)  # may raise 400
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
        )
        wire = ValidateResponse(
            status="pending", verdict=None, reasons=resolved.reasons, job_id=job_id
        )
    elif request.document_type == "carte_identite":
        wire = _handle_ci_submission(
            files, request.document_type, request.request_id
        )  # may raise 500
    else:
        wire = _handle_single_document(
            files, request.document_type, request.request_id
        )  # may raise 500
    if resolved.execution_mode == EXECUTION_MODE_SYNC and resolved.reasons:
        wire.reasons = [
            *wire.reasons,
            *resolved.reasons,
        ]  # e.g. an ignored hint, traced

    if request.request_id:
        _idempotency_cache[request.request_id] = wire
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
        _review_repository = SqliteReviewRepository(STORE_PATH, check_same_thread=False)
    return _review_repository


def _ensure_template_repository() -> TemplateRepository:
    """Build the shared D2 store once (lazy). Caller MUST hold `_repository_lock`.

    The committed `templates/*.json` are re-seeded on first use (idempotent upsert):
    the files stay the anonymized SEED, D2 is the runtime source the router reads."""
    global _template_repository
    if _template_repository is None:
        _template_repository = SqliteTemplateRepository(
            STORE_PATH, check_same_thread=False
        )
        _template_repository.seed_from_directory(TEMPLATES_DIRECTORY)
    return _template_repository


_execution_policy_repository: ExecutionPolicyRepository | None = None


def _ensure_execution_policy_repository() -> ExecutionPolicyRepository:
    """Build the shared execution-policy store once (lazy). Caller MUST hold `_repository_lock`.

    In-code defaults are seeded on first use (MISSING rows only): an operator edit made
    through /policies survives restarts, the doctrine of the config surface."""
    global _execution_policy_repository
    if _execution_policy_repository is None:
        _execution_policy_repository = SqliteExecutionPolicyRepository(
            STORE_PATH, check_same_thread=False
        )
        _execution_policy_repository.seed_defaults()
    return _execution_policy_repository


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def upload_page() -> str:
    return (UI_DIRECTORY / "upload.html").read_text(encoding="utf-8")


@app.get("/policies", response_class=HTMLResponse, include_in_schema=False)
def policies_page() -> str:
    return (UI_DIRECTORY / "policies.html").read_text(encoding="utf-8")


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
                }
            )
    return {"jobs": payload}


class DecisionRequest(BaseModel):
    """The human's verdict on a reviewed record — written to D3, never to D1."""

    decision: Literal["accept", "reject"]
    comment: str | None = None


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
    checks that become REQUIRED. The human selects among candidates, never authors rules
    here (curation surface). Absent body = every candidate stays required."""

    required: list[dict] | None = None


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
                rejected_rules = [
                    rule for rule in request.required if rule not in candidates
                ]
                if rejected_rules:
                    raise HTTPException(
                        status_code=400,
                        detail=f"not draft candidates: {rejected_rules}",
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
