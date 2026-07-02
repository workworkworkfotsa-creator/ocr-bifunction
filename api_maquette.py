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
from pydantic import BaseModel, Field

from ocr_bifunction.pipeline import CiSubmissionResult, process_ci_submission
from ocr_bifunction.reader import OcrEngine
from ocr_bifunction.repository import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_RECEIVED,
    Job,
    Repository,
    SqliteRepository,
)
from ocr_bifunction.router import RoutedDocument, route_document

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
    decided synchronously (no VLM escalation). `incomplete` (200): a CI side is missing
    (`missing` says which). `unrecognized` (200): not a recognizable document.
    """

    status: Literal[
        "validated", "pending", "needs_review", "incomplete", "unrecognized"
    ]
    verdict: Literal["auto", "human"] | None
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
    verdict: Literal["auto", "human"] | None = None
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
) -> int:
    """Persist the uploaded bytes to the spool and insert a `received` D1 row pointing at them.

    That row IS the queue entry (the status column is the signal): the watchdog worker — a
    separate process — claims it, re-runs the submission WITH the VLM from the spooled files
    (`document_ref`), writes the terminal state and purges the spool directory (PII hygiene)."""
    spool_directory = SPOOL_ROOT / f"sub_{uuid.uuid4().hex[:12]}"
    spool_directory.mkdir(parents=True, exist_ok=False)
    _write_files(files, spool_directory)
    source = ", ".join(filename for filename, _ in files)
    return _save_job(
        Job(
            source=source,
            category_lane="ci",
            status=STATUS_RECEIVED,
            execution_lane="escalation",
            category=category,
            reasons=fast_reasons,  # WHY the fast path doubted, visible while pending
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
    """Route the first uploaded file through the 2-lane router (non-CI document types)."""
    filename, data = files[0]
    suffix = Path(filename).suffix or DEFAULT_SUFFIX
    with tempfile.TemporaryDirectory(prefix="ocr_bifunction_doc_") as temp_directory:
        file_path = Path(temp_directory) / f"file{suffix}"
        file_path.write_bytes(data)
        try:
            return route_document(
                file_path, TEMPLATES_DIRECTORY, _get_engine(), category=category
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


def _handle_single_document(
    files: list[tuple[str, bytes]], document_type: str | None, request_id: str | None
) -> ValidateResponse:
    """Non-CI document types: validate ONE doc via the 2-lane router (no VLM escalation).

    Structured + auto -> validated; structured + human -> needs_review; non-structured
    (RAG lane) -> needs_review (a human handles it). Multiple files: only the first is used.
    EVERY outcome leaves a D1 row; the sync needs_review rows are what the review UI lists.
    """
    routed = _run_route_document(files, document_type)
    source = files[0][0]  # the original filename (routed.source is the temp name)
    if routed.lane == "structured":
        is_auto = routed.verdict == "auto"
        job_id = _save_job(
            Job(
                source=source,
                category_lane="structured",
                status=STATUS_DONE if is_auto else STATUS_NEEDS_REVIEW,
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
            status="validated" if is_auto else "needs_review",
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
    """Validate one upload, dispatched by `document_type` to the flow it declares.

    `carte_identite` -> CI submission (validated / pending / incomplete+missing /
    unrecognized). Any other type (facture, preuve_test, …) or none -> a single document
    through the 2-lane router (validated / needs_review). The hint says what the document
    is *supposed* to be, so the API runs the matching flow instead of assuming a CI.
    """
    if request.request_id and request.request_id in _idempotency_cache:
        cached = _idempotency_cache[request.request_id]
        response.status_code = _http_status_for(cached)
        return cached

    files = _decode_files(request)  # may raise 400
    if request.document_type == "carte_identite":
        wire = _handle_ci_submission(
            files, request.document_type, request.request_id
        )  # may raise 500
    else:
        wire = _handle_single_document(
            files, request.document_type, request.request_id
        )  # may raise 500

    if request.request_id:
        _idempotency_cache[request.request_id] = wire
    response.status_code = _http_status_for(wire)
    return wire


@app.get("/v1/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: int) -> JobResponse:
    """Poll an escalation job by its D1 row id. 404 if unknown. Maps the D1 lifecycle
    (received/processing/done/needs_review/failed) to the client's pending|done view — every
    terminal state reads as `done` (the async work is finished; `verdict` says auto vs human)."""
    job = _get_job_row(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    client_status = (
        "done"
        if job.status in (STATUS_DONE, STATUS_NEEDS_REVIEW, STATUS_FAILED)
        else "pending"
    )
    return JobResponse(
        status=client_status,  # type: ignore[arg-type]
        verdict=job.verdict,  # type: ignore[arg-type]
        reasons=job.reasons,
    )
