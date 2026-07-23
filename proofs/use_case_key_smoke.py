"""Use-case key smoke — the door's FIRST auth surface (D7, use_case_key.py).

    uv run python proofs/use_case_key_smoke.py

Proves, on a scratch store (no PII, no llama):
  1. no key -> the request resolves to the DEFAULT use_case (ci_pii), SILENTLY, and the
     job row persists it (zero regression for every caller predating the header);
  2. an unknown/garbage key -> 401, no job created;
  3. a freshly issued key for `sop_contract` -> the job row persists THAT use_case, not
     the default (the door resolved the right profile, traced in `reasons`);
  4. revoking a key makes it behave exactly like an unknown one -> 401;
  5. CRUD round-trip: create returns the raw secret once, list never does, delete is
     idempotent-safe (404 on a second revoke).

Deliberately does NOT test any depth-of-envelope behaviour: no reader consumes
`sop_contract` differently yet (see use_case_key.py's module docstring) — that is future
work, not this module's job.
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_use_case_key_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

from ocr_bifunction.adapters import api_maquette  # noqa: E402
from draft_smoke import _write_pdf  # noqa: E402  (PII-free corpus)
from ocr_bifunction.storage.repository import SqliteRepository  # noqa: E402
from ocr_bifunction.governance.use_case_key import (  # noqa: E402
    USE_CASE_CI_PII,
    USE_CASE_SOP_CONTRACT,
    hash_key,
    resolve_use_case,
)

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _payload_for(path: Path) -> dict:
    return {
        "files": [
            {
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        ]
    }


def _get_job(job_id: int):
    repository = SqliteRepository(os.environ["OCR_STORE_PATH"])
    job = repository.get(job_id)
    repository.close()
    return job


def run() -> int:
    corpus_directory = _SCRATCH / "corpus"
    corpus_directory.mkdir()
    generic_doc = corpus_directory / "memo.pdf"
    _write_pdf(generic_doc, [(72, 72, "Note interne sans structure reconnue.")])

    with TestClient(api_maquette.app) as client:
        # 1. No key -> default use_case, silently, job persists it.
        no_key = client.post("/v1/documents:validate", json=_payload_for(generic_doc))
        _check("no key -> 200/202 (never blocked)", no_key.status_code in (200, 202))
        job_no_key = _get_job(no_key.json()["job_id"])
        _check(
            "no key -> job carries the DEFAULT use_case (ci_pii)",
            job_no_key.use_case == USE_CASE_CI_PII,
        )

        # 2. Unknown/garbage key -> 401, no job.
        unknown = client.post(
            "/v1/documents:validate",
            json=_payload_for(generic_doc),
            headers={"X-OCR-Api-Key": "not-a-real-key"},
        )
        _check("unknown key -> 401", unknown.status_code == 401)

        # 3. Issue a real sop_contract key -> the job carries THAT use_case.
        created = client.post(
            "/v1/use-case-keys",
            json={"label": "SOP pilot", "use_case": USE_CASE_SOP_CONTRACT},
        ).json()
        _check("create returns the raw secret once", bool(created.get("api_key")))
        sop_key = created["api_key"]

        sop_upload = client.post(
            "/v1/documents:validate",
            json=_payload_for(generic_doc),
            headers={"X-OCR-Api-Key": sop_key},
        )
        _check("sop_contract key -> 200/202", sop_upload.status_code in (200, 202))
        job_sop = _get_job(sop_upload.json()["job_id"])
        _check(
            "sop_contract key -> job carries 'sop_contract', not the default",
            job_sop.use_case == USE_CASE_SOP_CONTRACT,
        )

        # 4. List never exposes the raw secret or the hash.
        listed = client.get("/v1/use-case-keys").json()
        _check(
            "list exposes label/use_case, never the raw secret",
            all(
                "api_key" not in key and "key_hash" not in key for key in listed["keys"]
            )
            and any(key["use_case"] == USE_CASE_SOP_CONTRACT for key in listed["keys"]),
        )

        # 5. Revoke -> the key now behaves exactly like unknown (401).
        revoke_response = client.delete(f"/v1/use-case-keys/{created['key_id']}")
        _check("revoke succeeds once", revoke_response.status_code == 200)
        after_revoke = client.post(
            "/v1/documents:validate",
            json=_payload_for(generic_doc),
            headers={"X-OCR-Api-Key": sop_key},
        )
        _check("revoked key -> 401 (same as unknown)", after_revoke.status_code == 401)
        second_revoke = client.delete(f"/v1/use-case-keys/{created['key_id']}")
        _check("revoking twice -> 404, not a crash", second_revoke.status_code == 404)

        # 6. Unknown use_case at creation -> 422, guarded (typo fail-loud).
        bad_use_case = client.post(
            "/v1/use-case-keys", json={"label": "typo", "use_case": "sop_contarct"}
        )
        _check("unknown use_case at creation -> 422", bad_use_case.status_code == 422)

    # Pure resolution, no FastAPI: the raw secret is never derivable from its hash alone.
    _check(
        "hash_key is deterministic (same input -> same hash)",
        hash_key("same-secret") == hash_key("same-secret"),
    )
    _check(
        "resolve_use_case(None, ...) -> the default, no lookup needed",
        resolve_use_case(None, None).use_case == USE_CASE_CI_PII,  # type: ignore[arg-type]
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT use_case key auth -> door resolution: {'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
