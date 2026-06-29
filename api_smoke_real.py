"""API maquette smoke — replay the DoD on a real CI pair, end to end.

    uv run python api_smoke_real.py <recto_image> <verso_image>
    uv run python api_smoke_real.py <recto_image> <verso_image> --expect validated

Drives the exact `validate_document` endpoint via TestClient (no server, no port), so it
exercises the real `process_ci_pair` behind the HTTP contract. Pass a concordant pair to
see `validated`/`auto`; cross a recto with another card's verso to see `needs_review`/
`human` with the mismatch reasons. With --expect, exits non-zero when the returned status
differs (so it can gate a check).

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


def validate_pair(recto_path: Path, verso_path: Path) -> tuple[int, dict]:
    """POST one recto+verso pair to the maquette and return (http_status, body)."""
    with TestClient(app) as client:
        response = client.post(
            "/v1/documents:validate",
            json={
                "filename": recto_path.name,
                "recto_base64": _encode_image(recto_path),
                "verso_base64": _encode_image(verso_path),
            },
        )
    return response.status_code, response.json()


def main(recto_path: Path, verso_path: Path, expected_status: str | None) -> int:
    http_status, body = validate_pair(recto_path, verso_path)

    print(f"recto   = {recto_path.name}")
    print(f"verso   = {verso_path.name}")
    print(f"HTTP    = {http_status}")
    print(f"status  = {body.get('status')}")
    print(f"verdict = {body.get('verdict')}")
    for reason in body.get("reasons", []):
        print(f"  - {reason}")

    if expected_status is None:
        return 0
    actual_status = body.get("status")
    passed = http_status == 200 and actual_status == expected_status
    print(
        f"\nEXPECT {expected_status}: {'PASS' if passed else 'FAIL'} (got {actual_status})"
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
        choices=["validated", "needs_review", "pending"],
        default=None,
        help="If set, exit non-zero unless the returned status matches.",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        main(arguments.recto_image, arguments.verso_image, arguments.expect)
    )
