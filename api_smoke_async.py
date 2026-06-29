"""API maquette async smoke — prove the escalation lane lifecycle on a real pair.

    uv run python api_smoke_async.py <recto_image> <verso_image>
    uv run python api_smoke_async.py <recto_2021> <verso_2021> --expect-escalation

Drives the real `validate_document` + worker via TestClient (no server, no port). A fast
FAKE escalation engine is injected so the async SHAPE runs WITHOUT paying the ~171 s VLM:
the fake records whether the worker reached the escalation tier and returns no lines. The
real VLM's MRZ recovery is proven separately (4/4 ICAO checksums, cf. HANDOFF) — this smoke
proves the LIFECYCLE the cadrage NEXT asked for:

    doubtful fast verdict -> 202 {"status": "pending", "job_id": ...}
                          -> worker drains the queue off the request path
                          -> GET /v1/jobs/{id} flips pending -> done

Pass a doubtful pair (recto A + verso B, OR a verso whose MRZ raw+enhance both miss). With
--expect-escalation the run also asserts the worker actually invoked the escalation engine
(use the missed-MRZ pair: a plain mismatch reads the verso MRZ raw, so escalation never
fires — correct, but the fake stays uncalled).

No PII lives in this file: the images come from the command line (the real inputs are
gitignored) and identity values appear only in the runtime output, never in the repo.
"""

from __future__ import annotations

import argparse
import base64
import time
from pathlib import Path

from fastapi.testclient import TestClient

import api_maquette
from api_maquette import app
from ocr_bifunction.reader import TextLine


class _FakeEscalationEngine:
    """A fast stand-in for the VLM. Records that the worker reached the escalation tier
    and returns no lines, so the async lifecycle runs in seconds, not minutes."""

    name = "fake-escalation"

    def __init__(self) -> None:
        self.called = False

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        self.called = True
        return []


def _encode_image(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


def run(
    recto_path: Path,
    verso_path: Path,
    document_type: str | None,
    expect_escalation: bool,
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 0.25,
) -> int:
    """POST a doubtful pair, then poll the job until done. Returns a process exit code."""
    fake_engine = _FakeEscalationEngine()
    api_maquette.set_escalation_engine_factory(lambda: fake_engine)

    payload: dict[str, object] = {
        "files": [
            {"filename": recto_path.name, "content_base64": _encode_image(recto_path)},
            {"filename": verso_path.name, "content_base64": _encode_image(verso_path)},
        ]
    }
    if document_type is not None:
        payload["document_type"] = document_type

    job: dict | None = None
    with TestClient(app) as client:
        post = client.post("/v1/documents:validate", json=payload)
        post_status, post_body = post.status_code, post.json()

        print(f"recto   = {recto_path.name}")
        print(f"verso   = {verso_path.name}")
        print(f"POST    = HTTP {post_status} / status {post_body.get('status')}")
        for reason in post_body.get("reasons", []):
            print(f"  - {reason}")

        job_id = post_body.get("job_id")
        if post_status != 202 or post_body.get("status") != "pending" or not job_id:
            print(
                "\nFAIL: expected 202 pending + job_id (doubtful -> escalation lane)."
            )
            return 1
        print(f"job_id  = {job_id}")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            job = client.get(f"/v1/jobs/{job_id}").json()
            if job.get("status") == "done":
                break
            time.sleep(poll_interval_seconds)

    print("\n-- job after polling --")
    print(f"status       = {job.get('status') if job else None}")
    print(f"verdict      = {job.get('verdict') if job else None}")
    print(f"verso_path   = {job.get('verso_read_path') if job else None}")
    for reason in (job or {}).get("reasons", []):
        print(f"  - {reason}")
    print(f"escalation engine called = {fake_engine.called}")

    completed = job is not None and job.get("status") == "done"
    passed = completed
    if expect_escalation:
        passed = passed and fake_engine.called

    label = "lifecycle + escalation fired" if expect_escalation else "lifecycle"
    print(f"\nEXPECT {label}: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prove the async escalation lane lifecycle on a real CI pair."
    )
    parser.add_argument("recto_image", type=Path, help="Path to the recto image.")
    parser.add_argument("verso_image", type=Path, help="Path to the verso image.")
    parser.add_argument(
        "--document-type",
        default=None,
        help="Optional document-type hint (e.g. carte_identite) scoping template match.",
    )
    parser.add_argument(
        "--expect-escalation",
        action="store_true",
        help="Also assert the worker invoked the escalation engine (use a missed-MRZ pair).",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        run(
            arguments.recto_image,
            arguments.verso_image,
            arguments.document_type,
            arguments.expect_escalation,
        )
    )
