"""The human's surface: the review queue, the field corrections, the suggestions.

WRITER CONTRACT — these endpoints READ D1 and WRITE D3 (decision, field correction,
suggestion validation), never D1.status; the watchdog closes D1 on its sweep, and it
is what applies an accepted correction to the record. Promotion writes D2."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel

from ocr_bifunction.knowledge.context_assembly import (
    ATTESTATION_REFERENCE_ROLES_KEY,
    REFERENCE_ROLE_FIELD_KEYS,
)
from ocr_bifunction.knowledge.promotion import promote_suggestion
from ocr_bifunction.storage.repository import (
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
)
from ocr_bifunction.storage.review_repository import (
    SUGGESTION_PENDING,
    SUGGESTION_REJECTED,
    Review,
)
from ocr_bifunction.extraction.template import (
    load_templates,
    payload_value,
)

from fastapi import APIRouter

from ocr_bifunction.adapters.api_maquette.settings import TEMPLATES_DIRECTORY
from ocr_bifunction.adapters.api_maquette.spool import _spooled_document_files
from ocr_bifunction.adapters.api_maquette.store_access import (
    _ensure_repository,
    _ensure_review_repository,
    _ensure_template_repository,
    _repository_lock,
)

router = APIRouter()


@router.get("/v1/reviews/queue")
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


@router.post("/v1/reviews/{job_id}/fields")
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


@router.post("/v1/reviews/{job_id}/decision")
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


@router.get("/v1/suggestions/pending")
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


@router.post("/v1/suggestions/{review_id}/validate")
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


@router.post("/v1/suggestions/{review_id}/reject")
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


@router.get("/v1/reviews/nonconformities")
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
