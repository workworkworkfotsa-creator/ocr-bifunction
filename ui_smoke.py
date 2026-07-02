"""UI + review surface smoke — drive the two pages' endpoints end to end (no llama).

    uv run python ui_smoke.py <facture_pdf> <courrier_pdf>

Proves, on a scratch store, the whole local-mix loop the pages skin (steps B+C of the brief):
  1. the pages are served (GET / and /review) and the select box source lists the categories;
  2. upload a real facture THROUGH THE SAME JSON the page's JS builds -> validated + a D1 row;
  3. upload a courrier (no match) -> needs_review -> it shows in /v1/reviews/queue;
  4. the human accepts it -> D3 decision; the REAL watchdog process sweeps -> the D1 job
     closes `done` and leaves the queue (the UI never wrote D1.status);
  5. a pending template suggestion (seeded as the live lane stages them) shows with its
     validation criteria (read-only v1) -> validate -> PROMOTED: active in D2, D3 validated.

No PII in this file: document paths come from the CLI; store/spool live in scratch.
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

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_ui_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

import api_maquette  # noqa: E402
from ocr_bifunction.review_repository import (  # noqa: E402
    Review,
    SqliteReviewRepository,
    Suggestion,
)
from ocr_bifunction.template_repository import SqliteTemplateRepository  # noqa: E402


def _payload_for(paths: list[Path], document_type: str | None = None) -> dict:
    """EXACTLY what the upload page's JS builds (FileReader base64 + optional select value)."""
    payload: dict[str, object] = {
        "files": [
            {
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
            for path in paths
        ]
    }
    if document_type:
        payload["document_type"] = document_type
    return payload


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


def run(facture_path: Path, courrier_path: Path) -> int:
    checks: list[tuple[str, bool]] = []
    with TestClient(api_maquette.app) as client:
        # 1. Pages served + select box source.
        upload_html = client.get("/")
        review_html = client.get("/review")
        types = client.get("/v1/document-types").json()["document_types"]
        checks.append(
            (
                "upload page served",
                upload_html.status_code == 200 and "document_type" in upload_html.text,
            )
        )
        checks.append(
            (
                "review page served",
                review_html.status_code == 200
                and "/v1/reviews/queue" in review_html.text,
            )
        )
        checks.append(
            ("select box lists categories", {"carte_identite", "facture"} <= set(types))
        )
        print(f"document types = {types}")

        # 2. Facture through the page's JSON -> validated + D1 row.
        facture = client.post(
            "/v1/documents:validate", json=_payload_for([facture_path])
        ).json()
        checks.append(
            (
                "facture -> validated/auto + job row",
                facture["status"] == "validated" and facture["job_id"] is not None,
            )
        )
        print(f"facture: {facture['status']} (job #{facture['job_id']})")

        # 3. Courrier -> needs_review -> visible in the queue.
        courrier = client.post(
            "/v1/documents:validate", json=_payload_for([courrier_path])
        ).json()
        queue_before = client.get("/v1/reviews/queue").json()["jobs"]
        in_queue = any(job["job_id"] == courrier["job_id"] for job in queue_before)
        checks.append(
            (
                "courrier -> needs_review in the queue",
                courrier["status"] == "needs_review" and in_queue,
            )
        )
        print(
            f"courrier: {courrier['status']} (job #{courrier['job_id']}), queue={len(queue_before)}"
        )

        # 4. Human accepts -> D3; the watchdog sweep closes D1 and empties the queue entry.
        decision = client.post(
            f"/v1/reviews/{courrier['job_id']}/decision",
            json={"decision": "accept", "comment": "readable, archive as-is"},
        ).json()
        print(f"decision recorded: review #{decision['review_id']}")
        sweep_output = _run_watchdog_once()
        swept = f"job #{courrier['job_id']}: closed done" in sweep_output
        queue_after = client.get("/v1/reviews/queue").json()["jobs"]
        gone = all(job["job_id"] != courrier["job_id"] for job in queue_after)
        checks.append(("watchdog sweep closed the accepted job", swept and gone))

        # 5. Pending suggestion (seeded like the live lane stages them) -> validate -> D2.
        review_repository = SqliteReviewRepository(os.environ["OCR_STORE_PATH"])
        seeded_review_id = review_repository.open_review(
            Review(
                job_id=courrier["job_id"],
                projection={"source": courrier_path.name, "lane": "structured"},
                suggestion=Suggestion(
                    template_id="facture_entrante_01",
                    category="facture",
                    anchors=["Sous-total", "Description"],
                ),
            )
        )
        review_repository.close()
        pending = client.get("/v1/suggestions/pending").json()["suggestions"]
        shows_criteria = any(
            s["review_id"] == seeded_review_id and s["validation"].get("required")
            for s in pending
        )
        checks.append(("pending suggestion shows validation criteria", shows_criteria))
        promoted = client.post(f"/v1/suggestions/{seeded_review_id}/validate").json()
        template_repository = SqliteTemplateRepository(os.environ["OCR_STORE_PATH"])
        d2_row = template_repository.get("facture_entrante_01")
        template_repository.close()
        checks.append(
            (
                "validate -> promoted active in D2",
                promoted.get("promoted_template_id") == "facture_entrante_01"
                and d2_row is not None,
            )
        )
        replay = client.post(f"/v1/suggestions/{seeded_review_id}/validate")
        checks.append(
            ("re-validate refused (409, already validated)", replay.status_code == 409)
        )

    print()
    passed = True
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        passed = passed and ok
    print(f"\nEXPECT UI + review surface: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Smoke the upload + review pages' endpoints end to end."
    )
    parser.add_argument("facture", type=Path, help="A real born-digital facture PDF.")
    parser.add_argument(
        "courrier", type=Path, help="A real no-match document (letter)."
    )
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.facture, arguments.courrier))
