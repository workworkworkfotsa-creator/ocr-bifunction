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

Two lanes, one door:
  - FAST PATH (in the request) — process_ci_submission with NO escalation engine.
  - ESCALATION (off the request path) — a complete-but-doubtful (`human`) submission is not
    returned synchronously: it enqueues a heavy re-run WITH the VLM and answers pending. A
    single serialized worker drains the queue (the ~171 s/img VLM must not run concurrently
    on the 8 GB target), flips the job pending->done, and the client polls GET /v1/jobs/{id}.

The in-memory `_jobs` dict and `queue.Queue` are the disposable proxy of the destination DB
"domain 1 — jobs + queue". IT swaps the store, queue, hosting, auth, TLS — disposable
adapters. The job/upload SHAPE is what survives the frontier.

Run it:
    uv run uvicorn api_maquette:app --reload
"""

from __future__ import annotations

import base64
import binascii
import queue
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field

from ocr_bifunction.pipeline import CiSubmissionResult, process_ci_submission
from ocr_bifunction.reader import OcrEngine

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"
# Default suffix when an uploaded filename carries none (a CI photo is usually one format).
DEFAULT_SUFFIX = ".jpg"

# Escalation runs the VLM (~171 s/img, ~1.8 GB RAM on the 8 GB target). It MUST stay
# serialized: a single worker drains the queue one job at a time. 1-2 is the safe band
# (2 VLMs ~= 3.6 GB) — IT owns this lever (cf. contrat-bd-destination.md, 4th surface).
ESCALATION_WORKER_COUNT = 1

app = FastAPI(
    title="OCR BiFunction — API maquette",
    version="1",
    description="Thin mock door over the CI submission pipeline. Not production.",
)

# --- Toy in-memory state (NOT production: lost on restart, never persisted). ---------
# `_jobs` is the disposable proxy of destination domain 1 (`ocr_jobs_*`). Mutated from the
# worker thread, read from request threads: whole-entry replacement is atomic under the GIL.
_jobs: dict[str, dict[str, object]] = {}
# Idempotency cache keyed by request_id: a replay returns the same result (same job_id).
_idempotency_cache: dict[str, "ValidateResponse"] = {}

# The fast OCR engine is heavy to build (loads ONNX models on CPU): built once, reused.
_engine: OcrEngine | None = None


def _get_engine() -> OcrEngine:
    global _engine
    if _engine is None:
        from ocr_bifunction.rapidocr_engine import RapidOcrEngine

        _engine = RapidOcrEngine()
    return _engine


# --- The escalation engine: the heavy VLM, built lazily, swappable for tests. ---------
EscalationEngineFactory = Callable[[], OcrEngine]


def _default_escalation_factory() -> OcrEngine:
    from ocr_bifunction.lightonocr_engine import LightOnOcrEngine

    return LightOnOcrEngine()


_escalation_engine: OcrEngine | None = None
_escalation_engine_factory: EscalationEngineFactory = _default_escalation_factory


def set_escalation_engine_factory(factory: EscalationEngineFactory) -> None:
    """Override the escalation engine (test/smoke seam). Resets the cached instance."""
    global _escalation_engine_factory, _escalation_engine
    _escalation_engine_factory = factory
    _escalation_engine = None


def _get_escalation_engine() -> OcrEngine:
    global _escalation_engine
    if _escalation_engine is None:
        _escalation_engine = _escalation_engine_factory()
    return _escalation_engine


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

    `validated` (200): complete + confident. `pending` (202): complete + doubtful, escalated
    async (poll job_id). `incomplete` (200): a side is missing (`missing` says which).
    `unrecognized` (200): not a CI submission.
    """

    status: Literal["validated", "pending", "incomplete", "unrecognized"]
    verdict: Literal["auto", "human"] | None
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)  # subset of ["recto", "verso"]
    job_id: str | None = None


class JobResponse(BaseModel):
    """The async follow-up. The worker flips a job pending->done; `verdict`/`reasons`
    carry the final escalated outcome (which may still be `human`)."""

    status: Literal["pending", "done"]
    verdict: Literal["auto", "human"] | None = None
    reasons: list[str] = Field(default_factory=list)
    verso_read_path: str | None = None


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


# --- Escalation lane: enqueue off the request path, drain with one serialized worker. -


@dataclass
class _EscalationJob:
    """One unit of escalation work, carrying the uploaded files so the worker re-runs the
    whole submission WITH the VLM. Bytes live in memory only until processed."""

    job_id: str
    files: list[tuple[str, bytes]]  # (filename, bytes)
    category: str | None


_escalation_queue: "queue.Queue[_EscalationJob]" = queue.Queue()
_workers_started = False
_workers_lock = threading.Lock()


def _ensure_workers_started() -> None:
    """Start the serialized escalation worker(s) once, lazily on first enqueue."""
    global _workers_started
    with _workers_lock:
        if _workers_started:
            return
        for _ in range(ESCALATION_WORKER_COUNT):
            threading.Thread(
                target=_escalation_worker, name="escalation-worker", daemon=True
            ).start()
        _workers_started = True


def _escalation_worker() -> None:
    """Drain the queue one job at a time, re-running the submission WITH the VLM engine."""
    while True:
        job = _escalation_queue.get()
        try:
            _jobs[job.job_id] = {**_jobs[job.job_id], "status": "processing"}
            record = _escalate(job)
            _jobs[job.job_id] = {
                "status": "done",
                "lane": "escalation",
                "verdict": record.verdict if record else None,
                "reasons": record.reasons
                if record
                else ["escalation produced no record"],
                "verso_read_path": record.verso_read_path if record else "none",
            }
        except Exception as error:  # surface the failure in the job, do not hide it
            _jobs[job.job_id] = {
                "status": "failed",
                "lane": "escalation",
                "verdict": None,
                "reasons": [f"escalation failure: {type(error).__name__}"],
                "verso_read_path": "none",
            }
        finally:
            _escalation_queue.task_done()


def _write_files(files: list[tuple[str, bytes]], directory: Path) -> list[Path]:
    """Write (filename, bytes) uploads to a directory, preserving each file's suffix."""
    paths: list[Path] = []
    for index, (filename, data) in enumerate(files):
        suffix = Path(filename).suffix or DEFAULT_SUFFIX
        file_path = directory / f"file_{index}{suffix}"
        file_path.write_bytes(data)
        paths.append(file_path)
    return paths


def _escalate(job: _EscalationJob):
    """Re-run the whole submission WITH the escalation engine wired in, on temp files."""
    # ignore_cleanup_errors: this runs in a daemon worker; if the process exits mid-job the
    # temp-dir rmtree can race interpreter shutdown on Windows (WinError 145). A transient
    # cleanup miss must not crash; the PII still goes when the dir is removed.
    with tempfile.TemporaryDirectory(
        prefix="ocr_bifunction_esc_", ignore_cleanup_errors=True
    ) as temp_directory:
        paths = _write_files(job.files, Path(temp_directory))
        result = process_ci_submission(
            paths,
            _get_engine(),
            TEMPLATES_DIRECTORY,
            category=job.category,
            escalation_engine=_get_escalation_engine(),
        )
        return result.record


def _enqueue_escalation(
    files: list[tuple[str, bytes]], category: str | None, fast_reasons: list[str]
) -> str:
    """Register a `received` job, enqueue it for the worker, and hand back its id."""
    _ensure_workers_started()
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    _jobs[job_id] = {
        "status": "received",
        "lane": "escalation",
        "verdict": None,
        "reasons": fast_reasons,  # WHY the fast path doubted, visible while pending
        "verso_read_path": "none",
    }
    _escalation_queue.put(_EscalationJob(job_id, files, category))
    return job_id


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


# --- Endpoints ------------------------------------------------------------------------


@app.post("/v1/documents:validate", response_model=ValidateResponse)
def validate_document(request: ValidateRequest, response: Response) -> ValidateResponse:
    """Validate one CI submission (any mix of images / a combined PDF).

    Complete + confident -> 200 validated. Complete + doubtful -> 202 pending (escalated
    async). A missing side -> 200 incomplete (+ `missing`). Not a CI -> 200 unrecognized.
    """
    if request.request_id and request.request_id in _idempotency_cache:
        cached = _idempotency_cache[request.request_id]
        response.status_code = _http_status_for(cached)
        return cached

    files = _decode_files(request)  # may raise 400
    result = _run_fast_submission(files, request.document_type)  # may raise 500

    if result.status == "complete" and result.record.verdict == "auto":
        wire = _map_complete_auto(result)
    elif (
        result.status == "complete"
    ):  # complete but doubtful -> escalate off the request
        job_id = _enqueue_escalation(
            files, request.document_type, result.record.reasons
        )
        wire = ValidateResponse(
            status="pending", verdict=None, reasons=result.record.reasons, job_id=job_id
        )
    else:  # incomplete | unrecognized -> the UI acts, no async work
        wire = _map_incomplete_or_unrecognized(result)

    if request.request_id:
        _idempotency_cache[request.request_id] = wire
    response.status_code = _http_status_for(wire)
    return wire


@app.get("/v1/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    """Poll an escalation job. 404 if unknown. Maps the internal lifecycle
    (`received`/`processing`/`done`/`failed`) to the client's pending|done view."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    internal_status = job["status"]
    client_status = "done" if internal_status in ("done", "failed") else "pending"
    return JobResponse(
        status=client_status,  # type: ignore[arg-type]
        verdict=job["verdict"],  # type: ignore[arg-type]
        reasons=job["reasons"],  # type: ignore[arg-type]
        verso_read_path=job.get("verso_read_path"),  # type: ignore[arg-type]
    )
