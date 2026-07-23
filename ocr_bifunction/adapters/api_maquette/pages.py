"""The local-test pages — plain HTML skins over the endpoints, zero business logic.

Disposable by doctrine: IT rebuilds these in its own dashboard. Nothing here decides
anything, which is exactly what makes them safe to throw away."""

from __future__ import annotations


from fastapi.responses import HTMLResponse


from fastapi import APIRouter

from ocr_bifunction.adapters.api_maquette.settings import UI_DIRECTORY
from ocr_bifunction.adapters.api_maquette.store_access import (
    _ensure_template_repository,
    _repository_lock,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def upload_page() -> str:
    return (UI_DIRECTORY / "upload.html").read_text(encoding="utf-8")


@router.get("/policies", response_class=HTMLResponse, include_in_schema=False)
def policies_page() -> str:
    return (UI_DIRECTORY / "policies.html").read_text(encoding="utf-8")


@router.get("/registry", response_class=HTMLResponse, include_in_schema=False)
def registry_page() -> str:
    return (UI_DIRECTORY / "registry.html").read_text(encoding="utf-8")


@router.get("/use-case-keys", response_class=HTMLResponse, include_in_schema=False)
def use_case_keys_page() -> str:
    return (UI_DIRECTORY / "use_case_keys.html").read_text(encoding="utf-8")


@router.get("/review", response_class=HTMLResponse, include_in_schema=False)
def review_page() -> str:
    return (UI_DIRECTORY / "review.html").read_text(encoding="utf-8")


@router.get("/v1/document-types")
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
