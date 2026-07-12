"""Conformity smoke — « document non conforme », its evidence, and the métier-configured
reaction (no OCR on images, no llama).

    uv run python conformity_smoke.py

Terminology + policy decisions (user, 2026-07-12): the machine proves NON-CONFORMITY
(fraud is compliance's judgment); the evidence is RETAINED and goes through the human
review; and the REACTION is config per category: block / block_holder /
flag_and_continue. On a scratch store, with the synthetic attestation template carrying
a reconcile_ci check (non-conformity on demand via a mismatched declared holder):

  1. default (block): non conforme -> rejected, bytes RETAINED, listed in the
     nonconformities queue with its documents;
  2. « clore » at the review -> the watchdog purges the evidence spool, status stays
     rejected (terminal);
  3. flag_and_continue: the same non-conformity FLAGS and routes to human review — the
     process continues, nothing blocked;
  4. block_holder: an open non-conformity for a declared holder REFUSES that holder's
     subsequent uploads (even a clean one), and clearing it at the review unblocks;
  5. declared-vs-recognized type mismatch: an attestation uploaded as 'facture' and as
     'carte_identite' -> non conforme « type mismatch » (the passport-as-CI case);
  6. async lane: the watchdog applies the same policy (flagged non-conformity ->
     needs_review);
  7. guards: unknown action 422, '*' undeletable 400; the /policies page carries the
     conformity table.

No PII in this file: fictional names, scratch store/spool.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_conformity_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

import api_maquette  # noqa: E402
from draft_smoke import _attestation_lines, _write_pdf  # noqa: E402  (PII-free corpus)
from ocr_bifunction.drafting import (  # noqa: E402
    DraftingDocument,
    cluster_unknown_documents,
    draft_from_cluster,
)
from ocr_bifunction.reader import read_document  # noqa: E402
from ocr_bifunction.repository import SqliteRepository  # noqa: E402
from ocr_bifunction.template_repository import SqliteTemplateRepository  # noqa: E402

_CORPUS = [
    ("FICTIF Alice", "12/03/2024", "12/03/2027", "H0B0 B1V", "DOSSIER-2024-0117"),
    ("EXEMPLE Bruno", "05/11/2023", "05/11/2026", "B1V BR", "DOSSIER-2023-0492"),
    ("SPECIMEN Chloe", "28/06/2024", "28/06/2027", "H0B0 BR", "DOSSIER-2024-0663"),
]

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _payload_for(
    path: Path, document_type: str, expected_holder_name: str | None = None
) -> dict:
    payload: dict[str, object] = {
        "files": [
            {
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        ],
        "document_type": document_type,
    }
    if expected_holder_name:
        payload["expected_holder_name"] = expected_holder_name
    return payload


def _stage_attestation_template(attestation_paths: list[Path]) -> None:
    """Attestation template with a reconcile_ci check: a mismatched declared holder is
    a non-conformity ON DEMAND (deterministic, no OCR)."""
    documents = []
    for path in attestation_paths:
        result = read_document(path, None)
        documents.append(
            DraftingDocument(source=path.name, text=result.text, lines=result.lines)
        )
    cluster = cluster_unknown_documents(documents)[0]
    report = draft_from_cluster(cluster, "attestation", "attestation_conformity_01")
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
            str(PROJECT_ROOT / "worker_watchdog.py"),
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
        # 1. Default (block): non conforme -> rejected, EVIDENCE RETAINED, queued.
        blocked = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "attestation", "EXEMPLE Bruno"),
        ).json()
        blocked_job = _get_job(blocked["job_id"])
        _check(
            "non conforme (block) -> rejected + bytes retained",
            blocked["status"] == "rejected"
            and blocked_job.document_ref is not None
            and Path(blocked_job.document_ref).is_dir(),
        )
        nonconformities = client.get("/v1/reviews/nonconformities").json()["jobs"]
        listed = next(
            (job for job in nonconformities if job["job_id"] == blocked["job_id"]), None
        )
        _check(
            "listed in the nonconformities queue with its document",
            listed is not None and len(listed["documents"]) == 1,
        )

        # 2. « Clore » -> the watchdog purges the evidence; status stays rejected.
        client.post(
            f"/v1/reviews/{blocked['job_id']}/decision",
            json={"decision": "accept", "comment": "transmis compliance"},
        )
        _run_watchdog_once()
        job_after_close = _get_job(blocked["job_id"])
        _check(
            "clore -> evidence spool purged, status stays rejected",
            job_after_close.status == "rejected"
            and not Path(blocked_job.document_ref).is_dir(),
        )

        # 3. flag_and_continue: flagged, routed to human review, nothing blocked.
        client.put(
            "/v1/conformity-policies/attestation",
            json={"action": "flag_and_continue"},
        )
        flagged = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "attestation", "EXEMPLE Bruno"),
        ).json()
        _check(
            "flag_and_continue -> needs_review with the flag (process continues)",
            flagged["status"] == "needs_review"
            and any("FLAGGED" in reason for reason in flagged["reasons"]),
        )

        # 4. block_holder: an open non-conformity refuses the holder's next uploads.
        client.put(
            "/v1/conformity-policies/attestation", json={"action": "block_holder"}
        )
        opening = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "attestation", "EXEMPLE Bruno"),
        ).json()
        clean_but_blocked = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[1], "attestation", "EXEMPLE Bruno"),
        ).json()
        _check(
            "open non-conformity blocks the holder's NEXT upload (even a clean one)",
            opening["status"] == "rejected"
            and clean_but_blocked["status"] == "rejected"
            and any(
                "dossier blocked" in reason for reason in clean_but_blocked["reasons"]
            ),
        )
        client.post(
            f"/v1/reviews/{opening['job_id']}/decision",
            json={"decision": "accept", "comment": "clos"},
        )
        _run_watchdog_once()
        unblocked = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[1], "attestation", "EXEMPLE Bruno"),
        ).json()
        _check(
            "clearing the non-conformity unblocks the holder -> clean doc validates",
            unblocked["status"] == "validated" and unblocked["verdict"] == "auto",
        )

        # 5. Declared-vs-recognized type mismatch (the passport-as-CI case).
        client.put("/v1/conformity-policies/attestation", json={"action": "block"})
        as_facture = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[2], "facture"),
        ).json()
        _check(
            "attestation declared 'facture' -> non conforme type mismatch",
            as_facture["status"] == "rejected"
            and any("type mismatch" in reason for reason in as_facture["reasons"]),
        )
        as_ci = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[2], "carte_identite"),
        ).json()
        _check(
            "attestation declared 'carte_identite' -> non conforme type mismatch",
            as_ci["status"] == "rejected"
            and any("type mismatch" in reason for reason in as_ci["reasons"]),
        )

        # 6. Async lane: the watchdog applies the same policy (flag -> needs_review).
        client.put(
            "/v1/conformity-policies/attestation",
            json={"action": "flag_and_continue"},
        )
        client.put(
            "/v1/execution-policies/attestation",
            json={"execution_mode": "async_immediate", "override_allowed": False},
        )
        deferred = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "attestation", "EXEMPLE Bruno"),
        ).json()
        _run_watchdog_once()
        deferred_job = _get_job(deferred["job_id"])
        _check(
            "async non-conformity obeys the policy: flagged -> needs_review",
            deferred["status"] == "pending"
            and deferred_job.status == "needs_review"
            and any("FLAGGED" in reason for reason in deferred_job.reasons),
        )

        # 7. Guards + page.
        _check(
            "unknown action refused (422)",
            client.put(
                "/v1/conformity-policies/attestation", json={"action": "explode"}
            ).status_code
            == 422,
        )
        _check(
            "'*' conformity default undeletable (400)",
            client.delete("/v1/conformity-policies/*").status_code == 400,
        )
        policies_page = client.get("/policies")
        _check(
            "policies page carries the conformity table",
            policies_page.status_code == 200
            and "/v1/conformity-policies" in policies_page.text,
        )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT non-conformity policy surface: {'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
