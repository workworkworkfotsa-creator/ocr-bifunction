"""API maquette smoke — drive a real CI submission end to end through the HTTP contract.

    uv run python proofs/api_smoke_real.py <recto> <verso>                 # -> validated
    uv run python proofs/api_smoke_real.py recto_verso.pdf --expect validated
    uv run python proofs/api_smoke_real.py <recto> --expect incomplete     # missing verso
    uv run python proofs/api_smoke_real.py <rectoA> <versoB> --expect pending

Drives the exact `validate_document` endpoint via TestClient (no server, no port), so it
exercises the real `process_ci_submission` behind the HTTP contract. One or more files form
one submission: a concordant pair (or a combined PDF) -> `validated` (200); a single side ->
`incomplete` (200) naming the missing side; recto of A + verso of B -> `pending` (202),
escalated async (cf. api_smoke_async.py). With --expect, exits non-zero on a status mismatch.

No PII lives in this file: paths come from the command line (the real inputs are gitignored)
and identity values appear only in the runtime output, never in the repo.
"""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

from fastapi.testclient import TestClient

from ocr_bifunction.adapters.api_maquette import app


def _encode_file(file_path: Path) -> str:
    return base64.b64encode(file_path.read_bytes()).decode("ascii")


def validate_submission(
    file_paths: list[Path], document_type: str | None = None
) -> tuple[int, dict]:
    """POST one submission (the given files) to the maquette and return (http_status, body)."""
    payload: dict[str, object] = {
        "files": [
            {"filename": file_path.name, "content_base64": _encode_file(file_path)}
            for file_path in file_paths
        ]
    }
    if document_type is not None:
        payload["document_type"] = document_type
    with TestClient(app) as client:
        response = client.post("/v1/documents:validate", json=payload)
    return response.status_code, response.json()


def main(
    file_paths: list[Path], expected_status: str | None, document_type: str | None
) -> int:
    http_status, body = validate_submission(file_paths, document_type)

    print(f"files   = {', '.join(path.name for path in file_paths)}")
    print(f"doc_type= {document_type}")
    print(f"HTTP    = {http_status}")
    print(f"status  = {body.get('status')}")
    print(f"verdict = {body.get('verdict')}")
    if body.get("missing"):
        print(f"missing = {body.get('missing')}")
    for reason in body.get("reasons", []):
        print(f"  - {reason}")

    if expected_status is None:
        return 0
    actual_status = body.get("status")
    # Only the escalated (pending) case is a 202; every other outcome is a 200.
    expected_http = 202 if expected_status == "pending" else 200
    passed = http_status == expected_http and actual_status == expected_status
    print(
        f"\nEXPECT {expected_status}: {'PASS' if passed else 'FAIL'} "
        f"(got {actual_status}, HTTP {http_status})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Drive the API maquette on a real CI submission (one or more files)."
    )
    parser.add_argument("files", type=Path, nargs="+", help="The uploaded file(s).")
    parser.add_argument(
        "--expect",
        choices=["validated", "pending", "needs_review", "incomplete", "unrecognized"],
        default=None,
        help="If set, exit non-zero unless the returned status matches.",
    )
    parser.add_argument(
        "--document-type",
        default=None,
        help="Optional document-type hint (e.g. carte_identite) scoping template match.",
    )
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.files, arguments.expect, arguments.document_type))
