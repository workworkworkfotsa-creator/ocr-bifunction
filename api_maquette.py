"""API maquette — a thin network door over the proven `process_ci_pair` pipeline.

This is a PEDAGOGICAL MOCK, not production. Its only job is to let the contract run
so we can *see* what "having an API" means: send a CI recto+verso pair, get back a
stable `{status, verdict, reasons, job_id}` envelope. The value lives in
`process_ci_pair`; this file only exposes it behind an HTTP door, exactly like
`OcrEngine` exposes an OCR engine behind an interface. The pipeline is NOT touched.

Two lanes, one door:
  - FAST PATH (in the request) — `process_ci_pair` with NO escalation engine. The
    confident pair comes back `200 validated` in seconds (RapidOCR, never the VLM).
  - ESCALATION (off the request path) — a `human`/doubtful verdict is NOT returned
    synchronously: the request enqueues a heavy re-run WITH the VLM escalation engine
    and answers `202 pending` + a `job_id`. A single serialized worker drains the
    queue (the ~171 s/img VLM must not run concurrently on the 8 GB target), flips the
    job `pending`->`done`, and the client polls `GET /v1/jobs/{id}` for the verdict.

This is the maquette of the destination DB "domain 1 — jobs + queue": the in-memory
`_jobs` dict and `queue.Queue` are the disposable proxy of the future `ocr_jobs_*`
table + worker. IT swaps the store (MariaDB), the queue mechanism, hosting, auth and
TLS — the disposable adapters. The job lifecycle SHAPE is what survives the frontier.

Run it:
    uv run uvicorn api_maquette:app --reload

Then open the auto-generated contract at http://127.0.0.1:8000/docs and try it there,
or call it directly (base64 the two images first):
    curl -X POST http://127.0.0.1:8000/v1/documents:validate \
         -H "Content-Type: application/json" \
         -d '{"filename": "ci.jpg", "recto_base64": "<...>", "verso_base64": "<...>",
              "request_id": "demo-1"}'

The three named cases:
  - a concordant pair        -> 200 {"status": "validated", "verdict": "auto"}
  - recto of A + verso of B   -> 202 {"status": "pending", "job_id": "job_..."}  (escalated)
  - a verso whose MRZ raw+enhance miss -> 202 pending, then the worker's VLM retry lands
    a verdict in the job; poll: curl http://127.0.0.1:8000/v1/jobs/<job_id>
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

from ocr_bifunction.pipeline import CiRecord, process_ci_pair
from ocr_bifunction.reader import OcrEngine

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"
# A pair carries one filename in the contract; recto and verso are written with the
# same suffix (a CI pair is normally photographed in one format). Falls back to .jpg.
DEFAULT_SUFFIX = ".jpg"

# Escalation runs the VLM (~171 s/img, ~1.8 GB RAM on the 8 GB target). It MUST stay
# serialized: a single worker drains the queue one job at a time. 1-2 is the safe band
# (2 VLMs ~= 3.6 GB) — IT owns this lever (cf. contrat-bd-destination.md, 4th surface).
ESCALATION_WORKER_COUNT = 1

app = FastAPI(
    title="OCR BiFunction — API maquette",
    version="1",
    description="Thin mock door over the CI recto+verso pipeline. Not production.",
)

# --- Toy in-memory state (NOT production: lost on restart, never persisted). ---------
# `_jobs` is the disposable proxy of destination domain 1 (`ocr_jobs_*`). Each entry
# carries the lifecycle status the future table holds; the client GET maps it to the
# simpler pending|done view. Mutated from the worker thread, read from request threads:
# whole-entry replacement is atomic under the GIL, enough for the maquette (no lock).
_jobs: dict[str, dict[str, object]] = {}
# Idempotency cache keyed by request_id: a replayed request_id returns the same result
# (same job_id for a pending escalation) without reprocessing. A toy, not production.
_idempotency_cache: dict[str, "ValidateResponse"] = {}

# The fast OCR engine is heavy to build (loads ONNX models on CPU), so it is constructed
# once on first use and reused across requests instead of per call.
_engine: OcrEngine | None = None


def _get_engine() -> OcrEngine:
    global _engine
    if _engine is None:
        # Imported lazily so app startup and /docs stay instant (no model load).
        from ocr_bifunction.rapidocr_engine import RapidOcrEngine

        _engine = RapidOcrEngine()
    return _engine


# --- The escalation engine: the heavy VLM, built lazily, swappable for tests. ---------
# The default is the proven LightOnOCR-2 slot. A smoke can inject a fast fake via
# `set_escalation_engine_factory` so the async SHAPE is exercised without the ~171 s VLM.
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


class ValidateRequest(BaseModel):
    """One CI recto+verso pair to validate. The pipeline works on a PAIR, so the
    contract honestly carries two files, not one."""

    filename: str = Field(description="Original name, used only for the file suffix.")
    recto_base64: str = Field(description="Recto image bytes, base64-encoded.")
    verso_base64: str = Field(description="Verso image bytes, base64-encoded.")
    document_type: str | None = Field(
        default=None,
        description=(
            "Optional document-type hint (e.g. 'carte_identite'). When the upload field "
            "already knows the type, template matching is scoped to that category only "
            "(an invoice template can never accidentally match). None tries every template."
        ),
    )
    request_id: str | None = Field(
        default=None,
        description="Idempotency key: replaying it returns the first result verbatim.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "filename": "ci.jpg",
                "recto_base64": "<base64 of the recto image>",
                "verso_base64": "<base64 of the verso image>",
                "document_type": "carte_identite",
                "request_id": "demo-1",
            }
        }
    }


class ValidateResponse(BaseModel):
    """Always this shape — never a mute response when something fails.

    `validated` (200) is the confident fast-path verdict. `pending` (202) means the
    doubtful case was handed to the async escalation lane; poll `job_id` for the result.
    """

    status: Literal["validated", "pending"]
    verdict: Literal["auto", "human"] | None
    reasons: list[str] = Field(default_factory=list)
    job_id: str | None = None


class JobResponse(BaseModel):
    """The async follow-up. The worker flips a job pending->done; `verdict`/`reasons`
    carry the final escalated outcome (which may still be `human`)."""

    status: Literal["pending", "done"]
    verdict: Literal["auto", "human"] | None = None
    reasons: list[str] = Field(default_factory=list)
    verso_read_path: str | None = None


# --- Mapping: the REAL record's verdict -> the contract. ------------------------------


def _map_record_to_response(record: CiRecord) -> ValidateResponse:
    """Translate a confident (auto) CiRecord into the wire contract. Only `auto` reaches
    here as a synchronous answer; the `human` case is routed to the escalation lane."""
    return ValidateResponse(
        status="validated", verdict="auto", reasons=record.reasons, job_id=None
    )


def _http_status_for(response: ValidateResponse) -> int:
    """202 says 'received, I'm working' for the escalation lane; 200 for a real verdict."""
    return (
        status.HTTP_202_ACCEPTED if response.status == "pending" else status.HTTP_200_OK
    )


# --- Escalation lane: enqueue off the request path, drain with one serialized worker. -


@dataclass
class _EscalationJob:
    """One unit of escalation work. Carries the image bytes so the worker is self-
    contained; the bytes live in memory only until processed (no extra disk PII)."""

    job_id: str
    recto_bytes: bytes
    verso_bytes: bytes
    suffix: str
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
    """Drain the queue one job at a time, re-running the pipeline WITH the VLM engine.

    Whatever the cascade decides (the VLM only fires if raw+enhance miss the MRZ), the
    job lands `done` with the final verdict, or `failed` if the engine/pipeline crashes —
    the error is recorded in the job, never swallowed silently.
    """
    while True:
        job = _escalation_queue.get()
        try:
            _jobs[job.job_id] = {**_jobs[job.job_id], "status": "processing"}
            record = _escalate(job)
            _jobs[job.job_id] = {
                "status": "done",
                "lane": "escalation",
                "verdict": record.verdict,
                "reasons": record.reasons,
                "verso_read_path": record.verso_read_path,
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


def _escalate(job: _EscalationJob) -> CiRecord:
    """Re-run the full pipeline WITH the escalation engine wired in, on temp files."""
    # ignore_cleanup_errors: this runs in a daemon worker; if the process exits mid-job
    # the temp-dir rmtree can race interpreter shutdown on Windows (WinError 145). The
    # PII still goes when the dir is removed; a transient cleanup miss must not crash.
    with tempfile.TemporaryDirectory(
        prefix="ocr_bifunction_esc_", ignore_cleanup_errors=True
    ) as temp_directory:
        temp_path = Path(temp_directory)
        recto_path = temp_path / f"recto{job.suffix}"
        verso_path = temp_path / f"verso{job.suffix}"
        recto_path.write_bytes(job.recto_bytes)
        verso_path.write_bytes(job.verso_bytes)
        return process_ci_pair(
            recto_path,
            verso_path,
            _get_engine(),
            TEMPLATES_DIRECTORY,
            category=job.category,
            escalation_engine=_get_escalation_engine(),
        )


def _enqueue_escalation(
    recto_bytes: bytes,
    verso_bytes: bytes,
    suffix: str,
    category: str | None,
    fast_reasons: list[str],
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
    _escalation_queue.put(
        _EscalationJob(job_id, recto_bytes, verso_bytes, suffix, category)
    )
    return job_id


# --- Fast path: decode + run the pipeline with no escalation, in the request. --------


def _decode_pair(request: ValidateRequest) -> tuple[bytes, bytes, str]:
    """Decode the base64 pair and pick a temp-file suffix. Raises HTTPException(400) on a
    bad request (4xx = caller's fault); the error is surfaced, never swallowed."""
    try:
        recto_bytes = base64.b64decode(request.recto_base64, validate=True)
        verso_bytes = base64.b64decode(request.verso_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=400, detail=f"invalid base64: {error}")
    if not recto_bytes or not verso_bytes:
        raise HTTPException(
            status_code=400, detail="recto_base64 and verso_base64 must be non-empty"
        )
    suffix = Path(request.filename).suffix or DEFAULT_SUFFIX
    return recto_bytes, verso_bytes, suffix


def _run_fast_pipeline(
    recto_bytes: bytes, verso_bytes: bytes, suffix: str, category: str | None
) -> CiRecord:
    """Run the real pipeline on temp files with NO escalation engine (the fast path).

    Temp files live under the system temp dir (never a versioned path) and the whole
    directory is removed on exit, so the PII does not linger on disk. Raises
    HTTPException(500) on a pipeline/engine crash (5xx = server's fault).
    """
    with tempfile.TemporaryDirectory(prefix="ocr_bifunction_api_") as temp_directory:
        temp_path = Path(temp_directory)
        recto_path = temp_path / f"recto{suffix}"
        verso_path = temp_path / f"verso{suffix}"
        recto_path.write_bytes(recto_bytes)
        verso_path.write_bytes(verso_bytes)
        try:
            return process_ci_pair(
                recto_path,
                verso_path,
                _get_engine(),
                TEMPLATES_DIRECTORY,
                category=category,
            )
        except Exception as error:  # surface a pipeline/engine crash as 5xx, don't hide
            raise HTTPException(
                status_code=500, detail=f"pipeline failure: {type(error).__name__}"
            )


# --- Endpoints ------------------------------------------------------------------------


@app.post("/v1/documents:validate", response_model=ValidateResponse)
def validate_document(request: ValidateRequest, response: Response) -> ValidateResponse:
    """Validate one CI recto+verso pair.

    The fast path never escalates. A confident pair returns `200 validated`; a doubtful
    one is handed to the async escalation lane and returns `202 pending` + a `job_id`.
    """
    if request.request_id and request.request_id in _idempotency_cache:
        cached = _idempotency_cache[request.request_id]
        response.status_code = _http_status_for(cached)
        return cached

    recto_bytes, verso_bytes, suffix = _decode_pair(request)  # may raise 400
    record = _run_fast_pipeline(  # may raise 500
        recto_bytes, verso_bytes, suffix, request.document_type
    )

    if record.verdict == "auto":
        result = _map_record_to_response(record)
    else:
        # Doubtful: escalate off the request path, answer pending. The page is not blocked
        # by the heavy VLM; the verdict lands in the job for the client to poll.
        job_id = _enqueue_escalation(
            recto_bytes, verso_bytes, suffix, request.document_type, record.reasons
        )
        result = ValidateResponse(
            status="pending", verdict=None, reasons=record.reasons, job_id=job_id
        )

    if request.request_id:
        _idempotency_cache[request.request_id] = result
    response.status_code = _http_status_for(result)
    return result


@app.get("/v1/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    """Poll an escalation job. 404 if unknown (4xx = caller asked for something not here).

    Maps the internal lifecycle (`received`/`processing`/`done`/`failed`) to the client's
    pending|done view: still working -> pending; finished (or failed) -> done.
    """
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
