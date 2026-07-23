"""Field-correction smoke — the reviewer edits an extracted value, D3 stages it, D1 gets it.

    uv run python proofs/field_correction_smoke.py

Seeing the zone lets a reviewer JUDGE a value; correcting it means WRITING one, and the writer
rule decides where: the UI writes D3, the watchdog writes D1. So a correction is staged as
`{field: {"from": what the machine read, "to": what the human put}}` and only lands in the
record when the review is accepted. Keeping both sides is what makes a correction auditable —
and what tells a recurring OCR weakness from a one-off.

PII-free: every value is fabricated here; no document is read (the D1 row is written directly).

Proves:
  1. re-submitting exactly what the machine read is NOT a correction (an untouched form is a
     no-op) — and a real edit IS staged, with both sides;
  2. staging does NOT touch D1: until the review is accepted the record still says, honestly,
     what the machine read;
  3. the watchdog's sweep applies it on ACCEPT — value replaced, `origin` = human, and spans
     EMPTIED (a typed value sits nowhere on the page; pointing at the machine's old box would
     show a region that no longer holds what the field says);
  4. the correction is SURGICAL: untouched fields keep their value, origin AND spans;
  5. a REJECTED review applies nothing (the record is not silently rewritten on the way out);
  6. an unknown field name is refused (422), not silently invented; an unknown job is 404;
  7. re-saving replaces the map, so un-editing a field removes its correction.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_field_correction_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

from ocr_bifunction.adapters import api_maquette  # noqa: E402
from ocr_bifunction.adapters import worker_watchdog  # noqa: E402
from ocr_bifunction.storage.repository import Job, SqliteRepository  # noqa: E402
from ocr_bifunction.storage.review_repository import SqliteReviewRepository  # noqa: E402
from ocr_bifunction.validation.status import STATUS_DONE, STATUS_NEEDS_REVIEW  # noqa: E402

CHECKS: list[tuple[str, bool]] = []

# A record as D1 stores it: value + origin + provenance spans (fabricated coordinates).
MACHINE_RECORD = {
    "numero_facture": {
        "value": "4271",
        "origin": "pattern",
        "spans": [{"page_index": 0, "bbox": [0.41, 0.08, 0.45, 0.09]}],
    },
    "total_ht": {
        "value": "409.74",
        "origin": "pattern",
        "spans": [{"page_index": 0, "bbox": [0.20, 0.28, 0.25, 0.30]}],
    },
}


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _new_job(repository: SqliteRepository, source: str) -> int:
    return repository.save(
        Job(
            source=source,
            category_lane="structured",
            category="facture",
            status=STATUS_NEEDS_REVIEW,
            verdict="review",
            record_fields={name: dict(entry) for name, entry in MACHINE_RECORD.items()},
        )
    )


def run() -> int:
    repository = SqliteRepository(os.environ["OCR_STORE_PATH"])
    accepted_job = _new_job(repository, "accepted.pdf")
    rejected_job = _new_job(repository, "rejected.pdf")
    client = TestClient(api_maquette.app)

    # --- 1. What counts as a correction. ----------------------------------------------
    unchanged = client.post(
        f"/v1/reviews/{accepted_job}/fields",
        json={"fields": {"total_ht": "409.74"}},
    ).json()
    _check(
        "re-submitting the machine's own value is not a correction",
        unchanged["corrected"] == [],
    )
    staged = client.post(
        f"/v1/reviews/{accepted_job}/fields",
        json={"fields": {"total_ht": "419.74"}},
    )
    _check(
        "a real edit is staged, naming the field",
        staged.status_code == 200 and staged.json()["corrected"] == ["total_ht"],
    )

    review_repository = SqliteReviewRepository(os.environ["OCR_STORE_PATH"])
    review = review_repository.by_job(accepted_job)
    _check(
        "D3 keeps BOTH sides — what the machine read and what the human put",
        review is not None
        and review.field_corrections
        == {"total_ht": {"from": "409.74", "to": "419.74"}},
    )

    # --- 2. Staging does not touch D1. ------------------------------------------------
    before = repository.get(accepted_job)
    _check(
        "D1 is UNTOUCHED while the correction is only staged (the UI never writes D1)",
        before.record_fields == MACHINE_RECORD and before.status == STATUS_NEEDS_REVIEW,
    )

    # --- 6. Refusals. -----------------------------------------------------------------
    _check(
        "a field name that is not in the record -> 422, never invented",
        client.post(
            f"/v1/reviews/{accepted_job}/fields", json={"fields": {"inexistant": "x"}}
        ).status_code
        == 422,
    )
    _check(
        "an unknown job -> 404",
        client.post("/v1/reviews/999999/fields", json={"fields": {}}).status_code
        == 404,
    )

    # --- 7. Re-saving replaces the map. -----------------------------------------------
    client.post(
        f"/v1/reviews/{accepted_job}/fields",
        json={"fields": {"total_ht": "419.74", "numero_facture": "4271"}},
    )
    review = review_repository.by_job(accepted_job)
    _check(
        "re-saving replaces: the untouched field leaves no correction behind",
        set(review.field_corrections) == {"total_ht"},
    )

    # --- 3+4+5. The watchdog applies it, and only on accept. --------------------------
    client.post(
        f"/v1/reviews/{accepted_job}/decision",
        json={"decision": "accept", "comment": None},
    )
    client.post(
        f"/v1/reviews/{rejected_job}/fields", json={"fields": {"total_ht": "999.99"}}
    )
    client.post(
        f"/v1/reviews/{rejected_job}/decision",
        json={"decision": "reject", "comment": None},
    )
    worker_watchdog._sweep_decisions(repository, review_repository)

    closed = repository.get(accepted_job)
    corrected = closed.record_fields["total_ht"]
    _check(
        "on ACCEPT the record carries the human value, origin=human, and NO span",
        closed.status == STATUS_DONE
        and corrected["value"] == "419.74"
        and corrected["origin"] == "human"
        and corrected["spans"] == [],
    )
    _check(
        "the correction is SURGICAL: the untouched field keeps value, origin and spans",
        closed.record_fields["numero_facture"] == MACHINE_RECORD["numero_facture"],
    )
    _check(
        "the reasons say a human corrected the field (traceable on the row itself)",
        any("human corrected 'total_ht'" in reason for reason in closed.reasons),
    )

    refused = repository.get(rejected_job)
    _check(
        "a REJECTED review applies nothing — the record is not rewritten on the way out",
        refused.record_fields == MACHINE_RECORD,
    )

    repository.close()
    review_repository.close()
    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT human correction (D3 stages, the watchdog applies): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
