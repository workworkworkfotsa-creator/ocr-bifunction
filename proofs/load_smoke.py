"""Load smoke — the door under concurrent fire: capped sync, graceful overflow (no OCR,
no llama).

    uv run python proofs/load_smoke.py

Worst-case doctrine (user, 2026-07-12): modest servers, so the sync lane is CAPPED and
the cap is a live lever. This smoke hammers the door with CONCURRENT uploads (threads,
same TestClient app) while the sync processing is artificially slowed (a patched
sync-handler wrapper — declared test scaffolding), and proves:

  1. the observed PEAK sync concurrency never exceeds SYNC_CONCURRENCY_LIMIT;
  2. overflow action `defer`: every upload beyond capacity gets 202 pending on the
     'deferred' lane — ZERO 500s, nothing lost; the watchdog then drains them all;
  3. overflow action `reject_503`: refusals carry HTTP 503 + Retry-After — still zero
     500s;
  4. the levers are live: PUT /v1/capacity-settings applies to the next upload;
  5. the idempotency cache stays BOUNDED under a flood of distinct request_ids.

No PII in this file: synthetic corpus, scratch store/spool.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ocr_bifunction.paths import ADAPTERS_DIRECTORY, PROJECT_ROOT

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_load_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

from ocr_bifunction.adapters import api_maquette  # noqa: E402
from draft_smoke import _attestation_lines, _write_pdf  # noqa: E402  (PII-free corpus)
from ocr_bifunction.knowledge.drafting import (  # noqa: E402
    DraftingDocument,
    cluster_unknown_documents,
    draft_from_cluster,
)
from ocr_bifunction.reading.reader import read_document  # noqa: E402
from ocr_bifunction.storage.repository import SqliteRepository  # noqa: E402
from ocr_bifunction.storage.template_repository import SqliteTemplateRepository  # noqa: E402

_CORPUS = [
    ("FICTIF Alice", "12/03/2024", "12/03/2027", "H0B0 B1V", "DOSSIER-2024-0117"),
    ("EXEMPLE Bruno", "05/11/2023", "05/11/2026", "B1V BR", "DOSSIER-2023-0492"),
    ("SPECIMEN Chloe", "28/06/2024", "28/06/2027", "H0B0 BR", "DOSSIER-2024-0663"),
]

CONCURRENT_UPLOADS = 12
SYNC_LIMIT = 2
SIMULATED_PROCESSING_SECONDS = 0.8

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _payload_for(path: Path, request_id: str | None = None) -> dict:
    payload: dict[str, object] = {
        "files": [
            {
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        ],
        "document_type": "attestation",
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


def _stage_attestation_template(attestation_paths: list[Path]) -> None:
    documents = []
    for path in attestation_paths:
        result = read_document(path, None)
        documents.append(
            DraftingDocument(source=path.name, text=result.text, lines=result.lines)
        )
    cluster = cluster_unknown_documents(documents)[0]
    report = draft_from_cluster(cluster, "attestation", "attestation_load_01")
    if report.template is None:
        raise RuntimeError(f"draft failed: {report.reasons}")
    template_repository = SqliteTemplateRepository(os.environ["OCR_STORE_PATH"])
    template_repository.upsert(report.template, active=True)
    template_repository.close()


class _ConcurrencyProbe:
    """Wrap the sync processing call with a sleep + a peak-concurrency counter — the test
    scaffolding that makes saturation deterministic and MEASURABLE."""

    def __init__(self, original) -> None:
        self._original = original
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def __call__(self, *args, **kwargs):
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            time.sleep(SIMULATED_PROCESSING_SECONDS)
            return self._original(*args, **kwargs)
        finally:
            with self._lock:
                self._active -= 1


def _fire_concurrent(client: TestClient, path: Path, count: int) -> list:
    responses: list = [None] * count
    threads = []
    for index in range(count):

        def _shoot(slot: int) -> None:
            responses[slot] = client.post(
                "/v1/documents:validate", json=_payload_for(path)
            )

        thread = threading.Thread(target=_shoot, args=(index,))
        threads.append(thread)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return responses


def _run_watchdog_once() -> str:
    completed = subprocess.run(
        [
            sys.executable,
            str(ADAPTERS_DIRECTORY / "worker_watchdog.py"),
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

    probe = _ConcurrencyProbe(api_maquette.door._handle_validated_document)
    api_maquette.door._handle_validated_document = probe

    with TestClient(api_maquette.app) as client:
        # 0. Levers live: set the cap + defer overflow.
        put_settings = client.put(
            "/v1/capacity-settings",
            json={
                "sync_concurrency_limit": SYNC_LIMIT,
                "sync_overflow_action": "defer",
            },
        )
        _check("PUT capacity levers (live config)", put_settings.status_code == 200)

        # 1+2. Concurrent fire, defer overflow: zero 500, everything lands somewhere.
        responses = _fire_concurrent(client, attestation_paths[0], CONCURRENT_UPLOADS)
        status_codes = [response.status_code for response in responses]
        bodies = [response.json() for response in responses]
        validated_count = sum(1 for body in bodies if body.get("status") == "validated")
        deferred = [body for body in bodies if body.get("status") == "pending"]
        _check(
            f"{CONCURRENT_UPLOADS} concurrent uploads -> ZERO 5xx",
            all(code in (200, 202) for code in status_codes),
        )
        _check(
            "observed sync concurrency peak <= configured limit "
            f"(peak={probe.peak}, limit={SYNC_LIMIT})",
            1 <= probe.peak <= SYNC_LIMIT,
        )
        _check(
            "every upload accounted for: sync validated + deferred = total",
            validated_count + len(deferred) == CONCURRENT_UPLOADS and len(deferred) > 0,
        )
        _check(
            "overflow trace on the deferred responses",
            all(
                any("capacity saturated" in reason for reason in body["reasons"])
                for body in deferred
            ),
        )
        repository = SqliteRepository(os.environ["OCR_STORE_PATH"])
        deferred_rows = [repository.get(body["job_id"]) for body in deferred]
        repository.close()
        _check(
            "deferred uploads are 'received' rows on the deferred lane (spooled)",
            all(
                row is not None
                and row.status == "received"
                and row.execution_lane == "deferred"
                for row in deferred_rows
            ),
        )

        # Drain: the watchdog absorbs the overflow — nothing is lost.
        _run_watchdog_once()
        repository = SqliteRepository(os.environ["OCR_STORE_PATH"])
        drained = [repository.get(body["job_id"]) for body in deferred]
        repository.close()
        _check(
            "watchdog drains the overflow -> all deferred jobs done/auto",
            all(
                row is not None and row.status == "done" and row.verdict == "auto"
                for row in drained
            ),
        )

        # 3. Overflow reject_503: refusals carry 503 + Retry-After, still zero 500.
        client.put(
            "/v1/capacity-settings",
            json={
                "sync_concurrency_limit": SYNC_LIMIT,
                "sync_overflow_action": "reject_503",
            },
        )
        responses_503 = _fire_concurrent(
            client, attestation_paths[1], CONCURRENT_UPLOADS
        )
        refused = [r for r in responses_503 if r.status_code == 503]
        _check(
            "reject_503 overflow: some 503s with Retry-After, zero 5xx other than 503",
            len(refused) > 0
            and all(r.headers.get("retry-after") for r in refused)
            and all(r.status_code in (200, 503) for r in responses_503),
        )

        # 4. Guards: a zero/negative cap is refused by the endpoint (422).
        _check(
            "cap < 1 refused (422)",
            client.put(
                "/v1/capacity-settings",
                json={"sync_concurrency_limit": 0, "sync_overflow_action": "defer"},
            ).status_code
            == 422,
        )

        # 5. Idempotency cache stays bounded under a flood of distinct request_ids.
        # The slow probe is removed (we test the EVICTION mechanism, not throughput)
        # and the cap is patched small so the flood stays cheap.
        api_maquette.door._handle_validated_document = probe._original
        api_maquette.door._IDEMPOTENCY_CACHE_MAX_ENTRIES = 20
        client.put(
            "/v1/capacity-settings",
            json={"sync_concurrency_limit": 8, "sync_overflow_action": "defer"},
        )
        for index in range(50):
            client.post(
                "/v1/documents:validate",
                json=_payload_for(attestation_paths[2], request_id=f"flood-{index}"),
            )
        _check(
            "idempotency cache bounded under request_id flood "
            f"(size={len(api_maquette.door._idempotency_cache)}, cap=20)",
            len(api_maquette.door._idempotency_cache) <= 20,
        )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT capped door under load: {'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
