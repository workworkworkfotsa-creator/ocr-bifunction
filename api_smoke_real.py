"""API maquette smoke — replay the DoD on a real CI pair, end to end.

    uv run python api_smoke_real.py <recto_image> <verso_image>
    uv run python api_smoke_real.py <recto_image> <verso_image> --expect validated

Drives the exact `validate_document` endpoint via TestClient (no server, no port), so it
exercises the real `process_ci_pair` behind the HTTP contract. Pass a concordant pair to
see `validated`/`auto` (200); cross a recto with another card's verso to see `pending`
(202) — the doubtful case is now handed to the async escalation lane, returning a job_id
to poll (cf. api_smoke_async.py for the full lifecycle). With --expect, exits non-zero
when the returned status differs (so it can gate a check).

No PII lives in this file: the images come from the command line (the real inputs are
gitignored) and identity values appear only in the runtime output, never in the repo.
"""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

from fastapi.testclient import TestClient

from api_maquette import app


def _encode_image(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


def validate_pair(
    recto_path: Path, verso_path: Path, document_type: str | None = None
) -> tuple[int, dict]:
    """POST one recto+verso pair to the maquette and return (http_status, body)."""
    payload: dict[str, object] = {
        "filename": recto_path.name,
        "recto_base64": _encode_image(recto_path),
        "verso_base64": _encode_image(verso_path),
    }
    if document_type is not None:
        payload["document_type"] = document_type
    with TestClient(app) as client:
        response = client.post("/v1/documents:validate", json=payload)
    return response.status_code, response.json()


def main(
    recto_path: Path,
    verso_path: Path,
    expected_status: str | None,
    document_type: str | None,
) -> int:
    http_status, body = validate_pair(recto_path, verso_path, document_type)

    print(f"recto   = {recto_path.name}")
    print(f"verso   = {verso_path.name}")
    print(f"doc_type= {document_type}")
    print(f"HTTP    = {http_status}")
    print(f"status  = {body.get('status')}")
    print(f"verdict = {body.get('verdict')}")
    for reason in body.get("reasons", []):
        print(f"  - {reason}")

    if expected_status is None:
        return 0
    actual_status = body.get("status")
    # validated is a synchronous 200; pending (escalated) is a 202 + job_id.
    expected_http = 202 if expected_status == "pending" else 200
    passed = http_status == expected_http and actual_status == expected_status
    print(
        f"\nEXPECT {expected_status}: {'PASS' if passed else 'FAIL'} "
        f"(got {actual_status}, HTTP {http_status})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Replay the API maquette DoD on a real CI pair."
    )
    parser.add_argument("recto_image", type=Path, help="Path to the recto image.")
    parser.add_argument("verso_image", type=Path, help="Path to the verso image.")
    parser.add_argument(
        "--expect",
        choices=["validated", "pending"],
        default=None,
        help="If set, exit non-zero unless the returned status matches.",
    )
    parser.add_argument(
        "--document-type",
        default=None,
        help="Optional document-type hint (e.g. carte_identite) scoping template match.",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        main(
            arguments.recto_image,
            arguments.verso_image,
            arguments.expect,
            arguments.document_type,
        )
    )
