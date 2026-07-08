"""Full-flow smoke — the COMPLETE loop from upload to promoted template, all through the
surfaces (no CLI side-channel, no OCR, no llama).

    uv run python flow_smoke.py

Proves, on a scratch store with a synthetic PII-free corpus (draft_smoke builders):
  1. three unknown attestations uploaded through the door -> needs_review, their bytes
     RETAINED (documents[] in the review queue payload; GET /v1/jobs/{id}/document 200);
  2. the NIGHTLY watchdog pass runs the DRAFT step: clusters the unknowns from D1,
     drafts a template, seeds CANDIDATE value checks from the cluster's own extractions
     (D-c part 2 deterministic: date_order + date_span + vocabulary), stages it in D3;
  3. PII guard holds: no holder name in the draft (anchors, allowed lists, anywhere);
  4. the pass is IDEMPOTENT (second nightly run stages nothing new);
  5. the reviewer TICKS a subset -> promotion D2 -> a fourth attestation re-matches at
     upload -> validated/auto, with dates normalized to ISO on the record (the seeded
     `normalize: date_ddmmyyyy` proved through extraction);
  6. a human decision closes a reviewed job at the sweep AND purges its spool;
  7. the issuer-registry surface (D-e plumbing): CRUD endpoints + /registry page.

No PII in this file: fictional names, scratch store/spool, no extracted values printed.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_flow_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

import api_maquette  # noqa: E402
from draft_smoke import _attestation_lines, _write_pdf  # noqa: E402  (PII-free corpus)
from ocr_bifunction.repository import SqliteRepository  # noqa: E402

# Codes are chosen so EVERY distinct token recurs in >=2 documents (the vocabulary
# candidate's PII guard demands recurrence) while the VALUES stay variant (the draft
# gate drops constants). Dates keep an exact 3-year span (date_span candidate).
_CORPUS = [
    ("FICTIF Alice", "12/03/2024", "12/03/2027", "H0B0 B1V", "DOSSIER-2024-0117"),
    ("EXEMPLE Bruno", "05/11/2023", "05/11/2026", "B1V BR", "DOSSIER-2023-0492"),
    ("SPECIMEN Chloe", "28/06/2024", "28/06/2027", "H0B0 BR", "DOSSIER-2024-0663"),
]
_FOURTH = ("MODELE David", "15/01/2024", "15/01/2027", "H0B0 BR", "DOSSIER-2024-0801")
# Holder identities (the PII the draft must never carry). "SPECIMEN" alone would
# collide with the ORGANISM anchor "CENTRE DE FORMATION SPECIMEN SAS" — an invariant
# issuing-body line, structural by doctrine — so the guard asserts on the holder
# VALUES (full names) and the per-doc first names.
_HOLDER_NAMES = [
    "FICTIF Alice",
    "EXEMPLE Bruno",
    "SPECIMEN Chloe",
    "Alice",
    "Bruno",
    "Chloe",
]

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _payload_for(path: Path, document_type: str | None = "attestation") -> dict:
    payload: dict[str, object] = {
        "files": [
            {
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        ]
    }
    if document_type:
        payload["document_type"] = document_type
    return payload


def _run_watchdog_once(nightly: bool) -> str:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "worker_watchdog.py"),
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


def _get_job(job_id: int):
    repository = SqliteRepository(os.environ["OCR_STORE_PATH"])
    job = repository.get(job_id)
    repository.close()
    return job


def run() -> int:
    corpus_directory = _SCRATCH / "corpus"
    corpus_directory.mkdir()
    attestation_paths: list[Path] = []
    for index, values in enumerate(_CORPUS):
        path = corpus_directory / f"attestation_{index + 1}.pdf"
        _write_pdf(path, _attestation_lines(*values))
        attestation_paths.append(path)
    fourth_path = corpus_directory / "attestation_4.pdf"
    _write_pdf(fourth_path, _attestation_lines(*_FOURTH))

    with TestClient(api_maquette.app) as client:
        # 1. Three unknowns through the door -> needs_review with retained bytes.
        uploads = [
            client.post("/v1/documents:validate", json=_payload_for(path)).json()
            for path in attestation_paths
        ]
        _check(
            "3 unknown attestations -> needs_review",
            all(upload["status"] == "needs_review" for upload in uploads),
        )
        queue = client.get("/v1/reviews/queue").json()["jobs"]
        documents_by_job = {job["job_id"]: job["documents"] for job in queue}
        _check(
            "review queue exposes the retained documents (url per file)",
            all(
                len(documents_by_job.get(upload["job_id"], [])) == 1
                for upload in uploads
            ),
        )
        first_document_url = documents_by_job[uploads[0]["job_id"]][0]["url"]
        served = client.get(first_document_url)
        _check(
            "GET /v1/jobs/{id}/document serves the original bytes",
            served.status_code == 200
            and served.content == attestation_paths[0].read_bytes(),
        )

        # 2. Nightly pass -> DRAFT step stages a D3 suggestion with candidate checks.
        nightly_output = _run_watchdog_once(nightly=True)
        for line in nightly_output.splitlines():
            if "draft pass" in line:
                print(f"  [watchdog] {line.strip()}")
        _check(
            "nightly pass staged the draft",
            "draft pass: staged 'draft_attestation_01'" in nightly_output,
        )
        pending = client.get("/v1/suggestions/pending").json()["suggestions"]
        draft_suggestion = next(
            (
                suggestion
                for suggestion in pending
                if suggestion["template_id"] == "draft_attestation_01"
            ),
            None,
        )
        _check("D3 pending suggestion carries the draft", draft_suggestion is not None)
        candidates = draft_suggestion["validation"]["required"]
        checks_present = {rule["check"] for rule in candidates}
        _check(
            "candidate checks seeded: present + date_order + date_span + vocabulary",
            {"present", "date_order", "date_span", "vocabulary"} <= checks_present,
        )
        vocabulary_rule = next(
            rule for rule in candidates if rule["check"] == "vocabulary"
        )
        _check(
            "vocabulary allowed = the recurring codes only",
            sorted(vocabulary_rule["allowed"]) == ["B1V", "BR", "H0B0"],
        )
        draft_as_text = json.dumps(draft_suggestion["draft_template"])
        _check(
            "PII guard: no holder name anywhere in the draft",
            not any(name in draft_as_text for name in _HOLDER_NAMES),
        )

        # 3. Idempotence: a second nightly pass stages nothing new.
        second_output = _run_watchdog_once(nightly=True)
        pending_after = client.get("/v1/suggestions/pending").json()["suggestions"]
        _check(
            "second nightly pass is idempotent (skip, no duplicate)",
            "idempotent skip" in second_output and len(pending_after) == len(pending),
        )

        # 4. Tick a subset (drop one presence check) -> promote -> 4th doc re-matches.
        ticked = [
            rule
            for rule in candidates
            if not (
                rule["check"] == "present" and rule["field"] == candidates[0]["field"]
            )
        ]
        promoted = client.post(
            f"/v1/suggestions/{draft_suggestion['review_id']}/validate",
            json={"required": ticked},
        ).json()
        _check(
            "ticked subset promoted to D2",
            promoted.get("promoted_template_id") == "draft_attestation_01",
        )
        fourth_upload = client.post(
            "/v1/documents:validate", json=_payload_for(fourth_path)
        ).json()
        fourth_job = _get_job(fourth_upload["job_id"])
        iso_dates = [
            value
            for value in (fourth_job.record_fields or {}).values()
            if isinstance(value, str)
            and len(value) == 10
            and value[4] == "-"
            and value[7] == "-"
        ]
        _check(
            "4th attestation re-matches -> validated/auto via the promoted draft",
            fourth_upload["status"] == "validated"
            and fourth_job.template_id == "draft_attestation_01",
        )
        _check(
            "seeded normalize proved: dates land ISO on the record",
            len(iso_dates) >= 2,
        )

        # 5. Human decision -> sweep closes the job AND purges its spool.
        reviewed_job_id = uploads[1]["job_id"]
        spool_directory = Path(_get_job(reviewed_job_id).document_ref)
        client.post(
            f"/v1/reviews/{reviewed_job_id}/decision",
            json={"decision": "accept", "comment": "checked against the document"},
        )
        _run_watchdog_once(nightly=False)
        _check(
            "decision sweep closed the job and purged its spool",
            _get_job(reviewed_job_id).status == "done" and not spool_directory.is_dir(),
        )

        # 6. Issuer registry surface (D-e plumbing): page + CRUD.
        registry_page = client.get("/registry")
        client.put(
            "/v1/issuer-registry/11122233344455",
            json={"label": "Centre de formation specimen"},
        )
        listed = client.get("/v1/issuer-registry").json()["issuers"]
        removed = client.delete("/v1/issuer-registry/11122233344455")
        _check(
            "issuer registry: page served + PUT/GET/DELETE round-trip",
            registry_page.status_code == 200
            and any(entry["identifier"] == "11122233344455" for entry in listed)
            and removed.status_code == 200
            and client.get("/v1/issuer-registry").json()["issuers"] == [],
        )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT full flow (upload -> draft -> ticked promotion -> re-match): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
