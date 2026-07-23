"""Declared-holder smoke — the manual document<->holder link feeds reconcile_ci (no OCR,
no llama).

    uv run python proofs/holder_reference_smoke.py

The D-e data decision (2026-07-08): the holder is DECLARED BY HAND at the door for now
(`expected_holder_name`); reading it from the validated CI record is a later upgrade.
On a scratch store, with a synthetic attestation template carrying a `reconcile_ci`
check (staged straight into D2 — the drafting of such templates is proven elsewhere):

  1. declared holder MATCHES the document -> validated/auto (strict identity, accents
     folded only);
  2. declared holder DIFFERS -> proven mismatch -> REJECTED (the sibling fraud,
     auto-terminal, no human);
  3. no declared holder -> reconcile_ci fails LOUD -> needs_review (never a false pass,
     never a false reject);
  4. the declared holder TRAVELS with an async job: policy async_immediate -> pending
     row carries it -> the watchdog's per-job context rejects the mismatch.

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

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_holder_smoke_"))
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


def _payload_for(path: Path, expected_holder_name: str | None) -> dict:
    payload: dict[str, object] = {
        "files": [
            {
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        ],
        "document_type": "attestation",
    }
    if expected_holder_name:
        payload["expected_holder_name"] = expected_holder_name
    return payload


def _stage_template_with_reconcile(attestation_paths: list[Path]) -> None:
    """Draft the synthetic attestations, add a reconcile_ci check on the holder field,
    and activate the result in D2 (the drafting/ticking path is proven in flow_smoke —
    here we only need a matching template that CARRIES the contextual check)."""
    documents = []
    for path in attestation_paths:
        result = read_document(path, None)
        documents.append(
            DraftingDocument(source=path.name, text=result.text, lines=result.lines)
        )
    cluster = cluster_unknown_documents(documents)[0]
    report = draft_from_cluster(cluster, "attestation", "attestation_reconcile_01")
    if report.template is None:
        raise RuntimeError(f"draft failed: {report.reasons}")
    holder_field_name = next(
        name
        for name, value in report.extractions_by_source[
            attestation_paths[0].name
        ].items()
        if value == "FICTIF Alice"
    )
    template = report.template
    template["validation"]["required"] = [
        *template["validation"]["required"],
        {"check": "reconcile_ci", "field": holder_field_name},
    ]
    template_repository = SqliteTemplateRepository(os.environ["OCR_STORE_PATH"])
    template_repository.upsert(template, active=True)
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
    _stage_template_with_reconcile(attestation_paths)

    with TestClient(api_maquette.app) as client:
        # 1. Declared holder matches -> auto.
        matching = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "FICTIF Alice"),
        ).json()
        _check(
            "declared holder matches -> validated/auto",
            matching["status"] == "validated" and matching["verdict"] == "auto",
        )

        # 2. Declared holder differs -> proven mismatch -> rejected (terminal).
        mismatched = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "EXEMPLE Bruno"),
        ).json()
        _check(
            "declared holder differs -> REJECTED (sibling fraud, no human)",
            mismatched["status"] == "rejected" and mismatched["verdict"] == "reject",
        )

        # 3. No declared holder -> fail-loud review.
        undeclared = client.post(
            "/v1/documents:validate", json=_payload_for(attestation_paths[0], None)
        ).json()
        _check(
            "no declared holder -> needs_review with the fail-loud reason",
            undeclared["status"] == "needs_review"
            and any("no CI reference" in reason for reason in undeclared["reasons"]),
        )

        # 4. The declared holder travels with an async job (per-job context).
        client.put(
            "/v1/execution-policies/attestation",
            json={"execution_mode": "async_immediate", "override_allowed": False},
        )
        deferred = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[1], "FICTIF Alice"),  # wrong holder
        )
        deferred_body = deferred.json()
        _check(
            "async job carries the declared holder on its D1 row",
            deferred.status_code == 202
            and _get_job(deferred_body["job_id"]).expected_holder_name
            == "FICTIF Alice",
        )
        _run_watchdog_once()
        job_after = _get_job(deferred_body["job_id"])
        _check(
            "watchdog per-job context rejects the async mismatch",
            job_after.status == "rejected" and job_after.verdict == "reject",
        )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT declared holder -> reconcile_ci: {'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
