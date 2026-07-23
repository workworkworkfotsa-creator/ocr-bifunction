"""Execution-policy smoke — prove the sync/async config surface end to end (no OCR, no llama).

    uv run python proofs/policy_smoke.py

Self-contained: the corpus is draft_smoke's synthetic PII-free attestations (born-digital,
text-layer, ZERO OCR), drafted into a matchable template staged straight into D2. On a
scratch store it proves:
  1. defaults seeded: '*' sync (override allowed) + carte_identite sync LOCKED;
  2. no policy row for a category -> '*' applies -> sync validated (behavior unchanged);
  3. policy async_nightly -> 202 pending, D1 row received/nightly/unrouted, bytes spooled;
  4. a plain watchdog pass does NOT touch the nightly lane; `--nightly` drains it: the job
     is ROUTED from the spool (structured/auto, template finalized on the row), spool purged;
  5. cohabitation of the API hint: hint IGNORED where the policy locks (trace in reasons),
     HONORED where override_allowed;
  6. async_immediate -> lane 'deferred', drained by the DEFAULT watchdog pass;
  7. delete a policy row -> the category falls back to '*';
  8. guards: '*' undeletable (400), unknown mode refused (422), unknown delete 404;
  9. the /policies page is served.

No PII in this file: synthetic corpus, scratch store/spool, no extracted values printed.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from ocr_bifunction.paths import ADAPTERS_DIRECTORY, PROJECT_ROOT

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_policy_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

from ocr_bifunction.adapters import api_maquette  # noqa: E402
from draft_smoke import _build_corpus  # noqa: E402  (synthetic PII-free corpus)
from ocr_bifunction.knowledge.drafting import (  # noqa: E402
    DraftingDocument,
    cluster_unknown_documents,
    draft_from_cluster,
)
from ocr_bifunction.reading.reader import read_document  # noqa: E402
from ocr_bifunction.storage.repository import SqliteRepository  # noqa: E402
from ocr_bifunction.storage.template_repository import SqliteTemplateRepository  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _payload_for(
    path: Path, document_type: str, processing_mode: str | None = None
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
    if processing_mode:
        payload["processing_mode"] = processing_mode
    return payload


def _get_job(job_id: int):
    repository = SqliteRepository(os.environ["OCR_STORE_PATH"])
    job = repository.get(job_id)
    repository.close()
    return job


def _run_watchdog_once(nightly: bool) -> str:
    command = [
        sys.executable,
        str(ADAPTERS_DIRECTORY / "worker_watchdog.py"),
        "--once",
        "--fake-escalation",
        "--store",
        os.environ["OCR_STORE_PATH"],
        "--pid-file",
        str(_SCRATCH / "watchdog.pid"),
    ]
    if nightly:
        command.append("--nightly")
    completed = subprocess.run(
        command, capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=600
    )
    return completed.stdout


def _stage_attestation_template(attestation_paths: list[Path]) -> None:
    """Draft the synthetic attestations into a template and activate it in D2 directly —
    the drafting lane itself is proven elsewhere (draft_smoke/ui_smoke); here we only
    need a MATCHABLE non-CI category to exercise the policies against."""
    documents = [
        DraftingDocument(source=path.name, text=result.text, lines=result.lines)
        for path in attestation_paths
        for result in [read_document(path, None)]
    ]
    clusters = cluster_unknown_documents(documents)
    cluster = next(cluster for cluster in clusters if len(cluster) >= 2)
    report = draft_from_cluster(cluster, "attestation", "draft_attestation_01")
    if report.template is None:
        raise RuntimeError(f"draft failed: {report.reasons}")
    template_repository = SqliteTemplateRepository(os.environ["OCR_STORE_PATH"])
    template_repository.upsert(report.template, active=True)
    template_repository.close()


def run() -> int:
    corpus_directory = _SCRATCH / "corpus"
    corpus_directory.mkdir()
    attestation_paths = [
        path
        for path in _build_corpus(corpus_directory)
        if path.name.startswith("attestation")
    ]
    _stage_attestation_template(attestation_paths)

    with TestClient(api_maquette.app) as client:
        # 1. Page + seeded defaults.
        policies_page = client.get("/policies")
        _check(
            "policies page served",
            policies_page.status_code == 200
            and "/v1/execution-policies" in policies_page.text,
        )
        listing = client.get("/v1/execution-policies").json()
        by_category = {policy["category"]: policy for policy in listing["policies"]}
        _check(
            "defaults seeded: '*' sync + override allowed",
            by_category.get("*", {}).get("execution_mode") == "sync"
            and by_category.get("*", {}).get("override_allowed") is True,
        )
        _check(
            "defaults seeded: carte_identite sync LOCKED",
            by_category.get("carte_identite", {}).get("execution_mode") == "sync"
            and by_category.get("carte_identite", {}).get("override_allowed") is False,
        )
        _check("3 execution modes exposed", len(listing["execution_modes"]) == 3)

        # 2. No own row -> '*' applies -> sync validated (unchanged behavior).
        sync_default = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "attestation"),
        ).json()
        _check(
            "no policy row -> '*' sync -> validated in the request",
            sync_default["status"] == "validated" and sync_default["verdict"] == "auto",
        )

        # 3. Policy async_nightly -> 202 pending, row received/nightly/unrouted, spooled.
        put_nightly = client.put(
            "/v1/execution-policies/attestation",
            json={"execution_mode": "async_nightly", "override_allowed": False},
        )
        _check("PUT policy attestation=async_nightly", put_nightly.status_code == 200)
        nightly_response = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[1], "attestation"),
        )
        nightly_body = nightly_response.json()
        nightly_job = _get_job(nightly_body["job_id"])
        _check(
            "async_nightly -> 202 pending + traced reason",
            nightly_response.status_code == 202
            and nightly_body["status"] == "pending"
            and any("async_nightly" in reason for reason in nightly_body["reasons"]),
        )
        _check(
            "D1 row: received / lane nightly / unrouted / bytes spooled",
            nightly_job is not None
            and nightly_job.status == "received"
            and nightly_job.execution_lane == "nightly"
            and nightly_job.category_lane == "unrouted"
            and nightly_job.document_ref is not None
            and Path(nightly_job.document_ref).is_dir(),
        )

        # 4. Plain pass leaves the nightly lane alone; --nightly drains + routes it.
        plain_output = _run_watchdog_once(nightly=False)
        job_after_plain = _get_job(nightly_body["job_id"])
        _check(
            "plain watchdog pass does NOT claim the nightly job",
            f"claimed job #{nightly_body['job_id']}" not in plain_output
            and job_after_plain.status == "received",
        )
        nightly_output = _run_watchdog_once(nightly=True)
        job_after_nightly = _get_job(nightly_body["job_id"])
        _check(
            "--nightly pass claims + routes it -> done/auto, row finalized",
            f"claimed job #{nightly_body['job_id']}" in nightly_output
            and job_after_nightly.status == "done"
            and job_after_nightly.verdict == "auto"
            and job_after_nightly.category_lane == "structured"
            and job_after_nightly.template_id == "draft_attestation_01",
        )
        _check(
            "spool purged after processing (PII hygiene)",
            not Path(nightly_job.document_ref).is_dir(),
        )
        polled = client.get(f"/v1/jobs/{nightly_body['job_id']}").json()
        _check(
            "GET /v1/jobs -> done/auto",
            polled["status"] == "done" and polled["verdict"] == "auto",
        )

        # 5. Cohabitation: hint IGNORED where locked, HONORED where override allowed.
        ignored = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[2], "attestation", "sync"),
        ).json()
        _check(
            "hint sync IGNORED (policy locks async_nightly) + trace",
            ignored["status"] == "pending"
            and any("ignored" in reason for reason in ignored["reasons"]),
        )
        client.put(
            "/v1/execution-policies/attestation",
            json={"execution_mode": "async_nightly", "override_allowed": True},
        )
        honored = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[2], "attestation", "sync"),
        ).json()
        _check(
            "hint sync HONORED once override allowed -> validated in the request",
            honored["status"] == "validated"
            and any("honored" in reason for reason in honored["reasons"]),
        )

        # 6. async_immediate -> lane 'deferred', drained by the DEFAULT pass.
        client.put(
            "/v1/execution-policies/attestation",
            json={"execution_mode": "async_immediate", "override_allowed": False},
        )
        deferred_body = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "attestation"),
        ).json()
        deferred_job = _get_job(deferred_body["job_id"])
        _check(
            "async_immediate -> pending, lane deferred",
            deferred_body["status"] == "pending"
            and deferred_job.execution_lane == "deferred",
        )
        default_output = _run_watchdog_once(nightly=False)
        job_after_default = _get_job(deferred_body["job_id"])
        _check(
            "default watchdog pass drains the deferred lane -> done/auto",
            f"claimed job #{deferred_body['job_id']}" in default_output
            and job_after_default.status == "done"
            and job_after_default.verdict == "auto",
        )

        # 7. Delete the row -> back to '*' -> sync again.
        deleted = client.delete("/v1/execution-policies/attestation")
        back_to_default = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[1], "attestation"),
        ).json()
        _check(
            "policy deleted -> category falls back to '*' sync",
            deleted.status_code == 200 and back_to_default["status"] == "validated",
        )

        # 8. Guards.
        _check(
            "'*' default row undeletable (400)",
            client.delete("/v1/execution-policies/*").status_code == 400,
        )
        _check(
            "unknown execution_mode refused (422)",
            client.put(
                "/v1/execution-policies/attestation",
                json={"execution_mode": "whenever", "override_allowed": False},
            ).status_code
            == 422,
        )
        _check(
            "delete of an absent policy row -> 404",
            client.delete("/v1/execution-policies/attestation").status_code == 404,
        )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT execution policy surface: {'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
