"""The contract, written black on white — this is what IT reads first.

The request and response envelopes, and nothing else: no store, no processing. A
reintegration in another stack reimplements THESE shapes and can ignore the rest."""

from __future__ import annotations

from typing import Literal

from fastapi import status
from pydantic import BaseModel, Field


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
