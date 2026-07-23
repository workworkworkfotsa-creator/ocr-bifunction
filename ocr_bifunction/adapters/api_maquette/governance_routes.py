"""The config surfaces a human owns — one CRUD block per lever, all read per request.

Execution policy (WHEN a category is processed), issuer registry (the métier list a
check reads), use-case keys (who is calling), conformity policy (what a proven
non-conformity does) and the capacity levers (the admission knobs). Every edit takes
effect on the NEXT upload: no restart, no redeploy — that is the point of a surface."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ocr_bifunction.governance.execution_policy import (
    DEFAULT_POLICY_CATEGORY,
    EXECUTION_MODES,
    ExecutionPolicy,
)
from ocr_bifunction.governance.capacity_settings import (
    DEFAULT_CAPACITY_SETTINGS,
    OVERFLOW_ACTIONS,
    load_capacity_settings,
)
from ocr_bifunction.governance.conformity_policy import (
    CONFORMITY_ACTIONS,
    ConformityPolicy,
    DEFAULT_CONFORMITY_CATEGORY,
)
from ocr_bifunction.governance.issuer_registry import (
    IssuerEntry,
)
from ocr_bifunction.governance.use_case_key import (
    KNOWN_USE_CASES,
)

from fastapi import APIRouter

from ocr_bifunction.adapters.api_maquette.store_access import (
    _ensure_capacity_settings_repository,
    _ensure_conformity_policy_repository,
    _ensure_execution_policy_repository,
    _ensure_issuer_registry_repository,
    _ensure_template_repository,
    _ensure_use_case_key_repository,
    _repository_lock,
)

router = APIRouter()

# --- Execution-policy surface: the /policies page's endpoints (reads the door obeys). ---


def _policy_payload(policy: ExecutionPolicy) -> dict:
    return {
        "category": policy.category,
        "execution_mode": policy.execution_mode,
        "override_allowed": policy.override_allowed,
        "updated_at": policy.updated_at,
    }


@router.get("/v1/execution-policies")
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


@router.put("/v1/execution-policies/{category}")
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


@router.delete("/v1/execution-policies/{category}")
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


@router.get("/v1/issuer-registry")
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


@router.put("/v1/issuer-registry/{identifier}")
def put_issuer_entry(identifier: str, request: IssuerEntryRequest) -> dict:
    """Add or relabel a recognized organism (SIRET preferred as identifier). Takes
    effect on the NEXT validation — the check reads the registry per request."""
    entry = IssuerEntry(identifier=identifier, label=request.label)
    with _repository_lock:
        _ensure_issuer_registry_repository().upsert(entry)
    return {"identifier": identifier, "label": request.label}


@router.delete("/v1/issuer-registry/{identifier}")
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


@router.get("/v1/use-case-keys")
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


@router.post("/v1/use-case-keys")
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


@router.delete("/v1/use-case-keys/{key_id}")
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


@router.get("/v1/conformity-policies")
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


@router.put("/v1/conformity-policies/{category}")
def put_conformity_policy(category: str, request: ConformityPolicyRequest) -> dict:
    """Create or update the reaction for a category ('*' = the default row). Takes
    effect on the NEXT upload — the door resolves per request."""
    policy = ConformityPolicy(category=category, action=request.action)
    with _repository_lock:
        _ensure_conformity_policy_repository().upsert(policy)
        saved = _ensure_conformity_policy_repository().get(category)
    return _conformity_policy_payload(saved) if saved else {"category": category}


@router.delete("/v1/conformity-policies/{category}")
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


@router.get("/v1/capacity-settings")
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


@router.put("/v1/capacity-settings")
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
