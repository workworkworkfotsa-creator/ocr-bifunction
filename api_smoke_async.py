"""API async smoke — prove the DOOR + WATCHDOG topology on a real pair (no llama).

    uv run python api_smoke_async.py <recto_image> <verso_image> [--document-type carte_identite]

Drives the REAL two-process shape the mix runs locally:

    POST (TestClient, real validate_document)  ->  202 pending + job_id
        the door spooled the bytes and wrote a D1 row status='received'
    worker_watchdog.py --once --fake-escalation  (a REAL SEPARATE PROCESS)
        claims the row atomically, re-runs the submission, writes the terminal state,
        purges the spool
    GET /v1/jobs/{id}  ->  done

The fake escalation engine (a watchdog flag, not an import seam) returns no lines, so the
async SHAPE runs in seconds without llama — the VLM's actual recovery quality was proven
separately (4/4 ICAO checksums, cf. HANDOFF). Escalation-reached is asserted from the
watchdog's output ("claimed job #<id>") — the WORKER did the work, not the request thread.

No PII in this file: images come from the command line (real inputs are gitignored) and
identity values appear only in runtime output. Store + spool live in a scratch directory.
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent

# The door reads store/spool locations from env AT IMPORT — set them to scratch BEFORE
# importing api_maquette so this smoke never touches the default local store.
_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_async_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

from api_maquette import app  # noqa: E402


def _encode_image(image_path: Path) -> str:
    return base64.b64encode(image_path.read_bytes()).decode("ascii")


def _run_watchdog_once() -> str:
    """Run the real watchdog process one pass (fake escalation engine); return its stdout."""
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
    print("-- watchdog output --")
    for line in completed.stdout.splitlines():
        print(f"   {line}")
    if completed.returncode != 0:
        print(f"   (exit {completed.returncode}) {completed.stderr[:400]}")
    return completed.stdout


def run(recto_path: Path, verso_path: Path, document_type: str | None) -> int:
    payload: dict[str, object] = {
        "files": [
            {"filename": recto_path.name, "content_base64": _encode_image(recto_path)},
            {"filename": verso_path.name, "content_base64": _encode_image(verso_path)},
        ]
    }
    if document_type is not None:
        payload["document_type"] = document_type

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

        watchdog_output = _run_watchdog_once()

        job = client.get(f"/v1/jobs/{job_id}").json()

    print("\n-- job after the watchdog pass --")
    print(f"status       = {job.get('status')}")
    print(f"verdict      = {job.get('verdict')}")
    for reason in job.get("reasons", []):
        print(f"  - {reason}")

    claimed_by_worker = f"claimed job #{job_id}" in watchdog_output
    completed = job.get("status") == "done"
    passed = completed and claimed_by_worker
    print(f"\nworker claimed the row : {claimed_by_worker}")
    print(f"EXPECT door->watchdog->done lifecycle: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prove the door + watchdog async lifecycle on a real CI pair."
    )
    parser.add_argument("recto_image", type=Path, help="Path to the recto image.")
    parser.add_argument("verso_image", type=Path, help="Path to the verso image.")
    parser.add_argument(
        "--document-type",
        default="carte_identite",
        help="Document-type hint (default carte_identite -> the CI submission flow).",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        run(arguments.recto_image, arguments.verso_image, arguments.document_type)
    )
