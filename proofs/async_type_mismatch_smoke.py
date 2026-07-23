"""Async type-mismatch smoke — the ONE behavior change of the worker cutover (candidate B).

    uv run python proofs/async_type_mismatch_smoke.py

Before the cutover the async worker's routed path (`_process_routed_job`) never ran the
declared-vs-recognized type-mismatch check: a document declared type A that actually matched a
type B template, pushed through an async lane, was simply routed to the RAG lane -> needs_review.
The door already caught this SYNC (a passport uploaded as a CI -> non conforme); the async lane
did not. Now BOTH entry points funnel through `intake.handle_document`, so the async worker
catches it too — the deliberate, grilled behavior change (HANDOFF decision B-4).

On a scratch store, with the synthetic attestation template staged and 'facture' set to
async_immediate:

  1. an attestation DECLARED 'facture' async -> the door only SPOOLS it (202 pending);
  2. the watchdog drains it and classifies it NON CONFORME « type mismatch » -> rejected
     (before the cutover: needs_review via the RAG lane);
  3. contrast/regression: an attestation DECLARED 'attestation' async -> the watchdog routes
     it structured and auto-validates it (the type-mismatch fires only on a real mismatch).

No PII in this file: fictional names, scratch store/spool.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ocr_bifunction.paths import ADAPTERS_DIRECTORY, PROJECT_ROOT

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_async_mismatch_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

from ocr_bifunction.adapters import api_maquette  # noqa: E402
from draft_smoke import _attestation_lines, _write_pdf  # noqa: E402  (PII-free corpus)
from ocr_bifunction.knowledge.drafting import (  # noqa: E402
    DraftingDocument,
    cluster_unknown_documents,
    draft_from_cluster,
)
from ocr_bifunction.reading.reader import read_document  # noqa: E402
from ocr_bifunction.storage.repository import SqliteRepository  # noqa: E402
from ocr_bifunction.storage.template_repository import SqliteTemplateRepository  # noqa: E402

_CORPUS = [
    ("FICTIF Alice", "12/03/2024", "12/03/2027", "H0B0 B1V", "DOSSIER-2024-0117"),
    ("EXEMPLE Bruno", "05/11/2023", "05/11/2026", "B1V BR", "DOSSIER-2023-0492"),
    ("SPECIMEN Chloe", "28/06/2024", "28/06/2027", "H0B0 BR", "DOSSIER-2024-0663"),
]

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _payload_for(path: Path, document_type: str) -> dict:
    return {
        "files": [
            {
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        ],
        "document_type": document_type,
    }


def _stage_attestation_template(attestation_paths: list[Path]) -> None:
    """A plain attestation template (no reconcile_ci): a matching attestation auto-validates,
    so the only non-conformity in this smoke is the declared-vs-recognized type mismatch."""
    documents = []
    for path in attestation_paths:
        result = read_document(path, None)
        documents.append(
            DraftingDocument(source=path.name, text=result.text, lines=result.lines)
        )
    cluster = cluster_unknown_documents(documents)[0]
    report = draft_from_cluster(cluster, "attestation", "attestation_async_mismatch_01")
    if report.template is None:
        raise RuntimeError(f"draft failed: {report.reasons}")
    template_repository = SqliteTemplateRepository(os.environ["OCR_STORE_PATH"])
    template_repository.upsert(report.template, active=True)
    template_repository.close()


def _get_job(job_id: int):
    repository = SqliteRepository(os.environ["OCR_STORE_PATH"])
    job = repository.get(job_id)
    repository.close()
    return job


def _run_watchdog_once() -> str:
    completed = subprocess.run(
        [
            sys.executable,
            str(ADAPTERS_DIRECTORY / "worker_watchdog.py"),
            "--once",
            "--fake-escalation",
            "--store",
            os.environ["OCR_STORE_PATH"],
            "--pid-file",
            str(_SCRATCH / "watchdog.pid"),
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        timeout=600,
    )
    return completed.stdout


def run() -> int:
    corpus_directory = _SCRATCH / "corpus"
    corpus_directory.mkdir()
    attestation_paths: list[Path] = []
    for index, values in enumerate(_CORPUS):
        path = corpus_directory / f"attestation_{index + 1}.pdf"
        _write_pdf(path, _attestation_lines(*values))
        attestation_paths.append(path)
    _stage_attestation_template(attestation_paths)

    with TestClient(api_maquette.app) as client:
        # 'facture' processed async_immediate (no facture template exists — the doc will
        # match no facture template and fall to the type-mismatch check in the worker).
        client.put(
            "/v1/execution-policies/facture",
            json={"execution_mode": "async_immediate", "override_allowed": False},
        )

        # 1. Declared 'facture' async -> the door only spools it (nothing processed sync).
        deferred = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "facture"),
        ).json()
        _check(
            "attestation declared 'facture' async -> door spools it (202 pending)",
            deferred["status"] == "pending" and deferred["verdict"] is None,
        )

        # 2. The watchdog catches the type mismatch -> non conforme rejected (B-4 change).
        _run_watchdog_once()
        mismatch_job = _get_job(deferred["job_id"])
        _check(
            "watchdog classifies the async type mismatch as non conforme -> rejected",
            mismatch_job.status == "rejected"
            and mismatch_job.verdict == "reject"
            and any("type mismatch" in reason for reason in mismatch_job.reasons),
        )
        _check(
            "the rejected row keeps its evidence (retained for review / compliance)",
            mismatch_job.document_ref is not None
            and Path(mismatch_job.document_ref).is_dir(),
        )

        # 3. Contrast: the SAME doc declared 'attestation' async -> matches, auto-validates
        #    (the type-mismatch check fires only on a genuine mismatch, no false positive).
        client.put(
            "/v1/execution-policies/attestation",
            json={"execution_mode": "async_immediate", "override_allowed": False},
        )
        matched = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[1], "attestation"),
        ).json()
        _run_watchdog_once()
        matched_job = _get_job(matched["job_id"])
        _check(
            "attestation declared 'attestation' async -> routed structured, auto (no false mismatch)",
            matched["status"] == "pending"
            and matched_job.status == "done"
            and matched_job.verdict == "auto"
            and matched_job.category_lane == "structured",
        )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT async type-mismatch caught by the worker: {'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
