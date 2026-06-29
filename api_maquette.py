"""API maquette — a thin network door over the proven `process_ci_pair` pipeline.

This is a PEDAGOGICAL MOCK, not production. Its only job is to let the contract run
so we can *see* what "having an API" means: send a CI recto+verso pair, get back a
stable `{status, verdict, reasons, job_id}` envelope. The value lives in
`process_ci_pair`; this file only exposes it behind an HTTP door, exactly like
`OcrEngine` exposes an OCR engine behind an interface. The pipeline is NOT touched.

Out of scope on purpose (IT territory): a real async worker, queue, hosting, auth,
TLS, scaling, job persistence. The `pending`/202 case is a deliberate STUB.

Run it:
    uv run uvicorn api_maquette:app --reload

Then open the auto-generated contract at http://127.0.0.1:8000/docs and try it there,
or call it directly (base64 the two images first):
    curl -X POST http://127.0.0.1:8000/v1/documents:validate \
         -H "Content-Type: application/json" \
         -d '{"filename": "ci.jpg", "recto_base64": "<...>", "verso_base64": "<...>",
              "request_id": "demo-1"}'

The three named cases:
  - a concordant pair                  -> 200 {"status": "validated", ...}
  - recto of A + verso of B            -> 200 {"status": "needs_review", "reasons": [...]}
  - "force_pending": true (debug flag) -> 202 {"status": "pending", "job_id": "job_..."}
    then: curl http://127.0.0.1:8000/v1/jobs/<job_id>
"""

from __future__ import annotations

import base64
import binascii
import tempfile
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field

from ocr_bifunction.pipeline import CiRecord, process_ci_pair
from ocr_bifunction.reader import OcrEngine

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"
# A pair carries one filename in the contract; recto and verso are written with the
# same suffix (a CI pair is normally photographed in one format). Falls back to .jpg.
DEFAULT_SUFFIX = ".jpg"

app = FastAPI(
    title="OCR BiFunction — API maquette",
    version="1",
    description="Thin mock door over the CI recto+verso pipeline. Not production.",
)

# --- Toy in-memory state (NOT production: lost on restart, never persisted). ---------
# Jobs created by the `pending` stub. With no real worker they stay "pending" forever;
# this only demonstrates the polling SHAPE of GET /v1/jobs/{id}.
_jobs: dict[str, dict[str, object]] = {}
# Idempotency cache keyed by request_id: a replayed request_id returns the same result
# without reprocessing. A plain dict is enough for the demo — a toy, not production.
_idempotency_cache: dict[str, "ValidateResponse"] = {}

# The OCR engine is heavy to build (loads ONNX models on CPU), so it is constructed
# once on first use and reused across requests instead of per call.
_engine: OcrEngine | None = None


def _get_engine() -> OcrEngine:
    global _engine
    if _engine is None:
        # Imported lazily so app startup and /docs stay instant (no model load).
        from ocr_bifunction.rapidocr_engine import RapidOcrEngine

        _engine = RapidOcrEngine()
    return _engine


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
    force_pending: bool = Field(
        default=False,
        description=(
            "DEBUG FLAG (maquette only): forces the 202/pending stub so the async "
            "shape is visible without a real heavy worker."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "filename": "ci.jpg",
                "recto_base64": "<base64 of the recto image>",
                "verso_base64": "<base64 of the verso image>",
                "document_type": "carte_identite",
                "request_id": "demo-1",
                "force_pending": False,
            }
        }
    }


class ValidateResponse(BaseModel):
    """Always this shape — never a mute response when something fails."""

    status: Literal["validated", "needs_review", "pending"]
    verdict: Literal["auto", "human"] | None
    reasons: list[str] = Field(default_factory=list)
    job_id: str | None = None


class JobResponse(BaseModel):
    """The async-form follow-up. With no worker wired in, a known job stays pending."""

    status: Literal["pending", "done"]
    verdict: Literal["auto", "human"] | None = None
    reasons: list[str] = Field(default_factory=list)


# --- Mapping: the REAL record's verdict -> the contract's status. ---------------------


def _map_record_to_response(record: CiRecord) -> ValidateResponse:
    """Translate a CiRecord verdict into the wire contract. The single source of the
    auto->validated / human->needs_review mapping."""
    if record.verdict == "auto":
        return ValidateResponse(
            status="validated", verdict="auto", reasons=record.reasons, job_id=None
        )
    # "human": the doubtful case, ruled by checksum / recto-verso mismatch.
    return ValidateResponse(
        status="needs_review",
        verdict="human",
        reasons=record.reasons,
        job_id=None,
    )


def _http_status_for(response: ValidateResponse) -> int:
    """202 says 'received, I'm working' for the pending stub; 200 for a real verdict."""
    return (
        status.HTTP_202_ACCEPTED if response.status == "pending" else status.HTTP_200_OK
    )


def _make_pending_response() -> ValidateResponse:
    """The deliberate STUB: register a job and hand back its id. No worker runs it."""
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    _jobs[job_id] = {"status": "pending", "verdict": None, "reasons": []}
    return ValidateResponse(status="pending", verdict=None, reasons=[], job_id=job_id)


def _run_pipeline(request: ValidateRequest) -> ValidateResponse:
    """Decode the pair, run the real pipeline on temp files, map the verdict back.

    Raises HTTPException(400) on a bad request (4xx = caller's fault) and
    HTTPException(500) on a pipeline/engine crash (5xx = server's fault) — the error
    is surfaced, never swallowed.
    """
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
    # Temp files live under the system temp dir (never a versioned path) and the whole
    # directory is removed on exit, so the PII does not linger on disk.
    with tempfile.TemporaryDirectory(prefix="ocr_bifunction_api_") as temp_directory:
        temp_path = Path(temp_directory)
        recto_path = temp_path / f"recto{suffix}"
        verso_path = temp_path / f"verso{suffix}"
        recto_path.write_bytes(recto_bytes)
        verso_path.write_bytes(verso_bytes)
        try:
            record = process_ci_pair(
                recto_path,
                verso_path,
                _get_engine(),
                TEMPLATES_DIRECTORY,
                category=request.document_type,
            )
        except Exception as error:  # surface a pipeline/engine crash as 5xx, don't hide
            raise HTTPException(
                status_code=500, detail=f"pipeline failure: {type(error).__name__}"
            )
    return _map_record_to_response(record)


# --- Endpoints ------------------------------------------------------------------------


@app.post("/v1/documents:validate", response_model=ValidateResponse)
def validate_document(request: ValidateRequest, response: Response) -> ValidateResponse:
    """Validate one CI recto+verso pair. 200 with a verdict, or 202 for the stub."""
    if request.request_id and request.request_id in _idempotency_cache:
        cached = _idempotency_cache[request.request_id]
        response.status_code = _http_status_for(cached)
        return cached

    if request.force_pending:
        result = _make_pending_response()
    else:
        result = _run_pipeline(request)  # may raise HTTPException(400|500)

    if request.request_id:
        _idempotency_cache[request.request_id] = result
    response.status_code = _http_status_for(result)
    return result


@app.get("/v1/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    """Poll a stub job. 404 if unknown (4xx = caller asked for something not here)."""
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id: {job_id}")
    return JobResponse(
        status=job["status"],  # type: ignore[arg-type]
        verdict=job["verdict"],  # type: ignore[arg-type]
        reasons=job["reasons"],  # type: ignore[arg-type]
    )
