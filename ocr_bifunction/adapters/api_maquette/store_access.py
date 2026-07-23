"""The stores behind the door, and the admission counter that caps it.

One lazily built singleton per surface (D1 jobs, D2 templates, D3 review, plus the
governance stores), all serialized by `_repository_lock` — the worker thread writes
`status` while request threads read and enqueue, one writer per phase. Swapping
SQLite for the real database means rewriting THIS module and nothing else."""

from __future__ import annotations

import threading


from ocr_bifunction.governance.execution_policy import (
    ExecutionPolicyRepository,
    SqliteExecutionPolicyRepository,
)
from ocr_bifunction.governance.capacity_settings import (
    CapacitySettings,
    CapacitySettingsRepository,
    SqliteCapacitySettingsRepository,
    load_capacity_settings,
)
from ocr_bifunction.governance.conformity_policy import (
    ConformityPolicyRepository,
    SqliteConformityPolicyRepository,
)
from ocr_bifunction.governance.issuer_registry import (
    IssuerRegistryRepository,
    SqliteIssuerRegistryRepository,
)
from ocr_bifunction.reading.reader import OcrEngine
from ocr_bifunction.storage.store import Store
from ocr_bifunction.storage.repository import (
    Job,
    Repository,
    SqliteRepository,
)
from ocr_bifunction.storage.review_repository import (
    ReviewRepository,
    SqliteReviewRepository,
)
from ocr_bifunction.storage.template_repository import (
    SqliteTemplateRepository,
    TemplateRepository,
)
from ocr_bifunction.governance.use_case_key import (
    SqliteUseCaseKeyRepository,
    UseCaseKeyRepository,
)

from ocr_bifunction.adapters.api_maquette.settings import (
    STORE_PATH,
    TEMPLATES_DIRECTORY,
)

# --- D1 store: the SAME `ocr_jobs` table the batch regime writes (repository.py). ----
# The escalation lifecycle (received -> processing -> done|needs_review|failed) now lives in
# D1, so this API and the batch orchestrator exercise ONE column contract. A single shared
# connection is opened lazily and every access is guarded by `_repository_lock`: the worker
# thread writes `status`, request threads read/enqueue — one writer per phase, no race.

_repository: Repository | None = None
_repository_lock = threading.Lock()
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
        from ocr_bifunction.reading.engines.rapidocr_engine import RapidOcrEngine

        _engine = RapidOcrEngine()
    return _engine


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
