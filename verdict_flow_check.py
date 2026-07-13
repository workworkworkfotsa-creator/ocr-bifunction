"""End-to-end wiring smoke — the three-state verdict travels through the real flow.

Deterministic, no machine, no OCR (born-digital synthetic attestations, PII-free). A template
carrying anti-fraud checks (date_span + vocabulary) is matched by the REAL route_document /
process_batch; proves:
  1. a clean attestation  -> verdict auto   -> BatchResult.auto     -> D1 status 'done';
  2. a pen-lengthened one  -> verdict reject -> BatchResult.rejected -> D1 status 'rejected';
  3. an invented-code one  -> verdict reject -> rejected;
  4. the sink bridge (_job_from_record) maps reject -> STATUS_REJECTED (terminal, no human).

Run from the project root: uv run python verdict_flow_check.py
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from batch_check import _job_from_record
from draft_smoke import _attestation_lines, _write_pdf
from ocr_bifunction.orchestrator import BatchItem, DocumentRecord, process_batch
from ocr_bifunction.repository import STATUS_DONE, STATUS_REJECTED
from ocr_bifunction.router import route_document

_TODAY = date(2026, 7, 3)

# A template that matches the synthetic attestation layout and carries anti-fraud checks.
_ATTESTATION_TEMPLATE = {
    "template_id": "test_attestation_fraud",
    "category": "attestation",
    "match": {"all_anchors": ["ATTESTATION DE FORMATION", "Habilitation electrique"]},
    "fields": [
        {
            "name": "date_of_issue",
            "anchor": "Delivree le",
            "direction": "below",
            "normalize": "date_ddmmyyyy",
        },
        {
            "name": "valid_until",
            "anchor": "Valable jusqu'au",
            "direction": "below",
            "normalize": "date_ddmmyyyy",
        },
        {"name": "codes", "anchor": "Codes obtenus", "direction": "below"},
    ],
    "validation": {
        "required": [
            {
                "check": "date_span",
                "start": "date_of_issue",
                "end": "valid_until",
                "years": 3,
            },
            {
                "check": "vocabulary",
                "field": "codes",
                "allowed": ["H0B0", "B1V", "B2V", "BR", "BC"],
            },
        ]
    },
}

_checks_passed = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _checks_passed
    if not condition:
        raise AssertionError(f"CHECK FAILED: {label} {detail}")
    _checks_passed += 1
    print(f"  PASS {label}")


def _write_attestation(path: Path, issue: str, expiry: str, codes: str) -> None:
    _write_pdf(path, _attestation_lines("SPECIMEN Demo", issue, expiry, codes, "REF-1"))


def _route(path: Path):
    return route_document(
        path,
        templates_directory=Path("."),
        templates=[_ATTESTATION_TEMPLATE],
        category="attestation",
        today=_TODAY,
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        clean = base / "clean.pdf"
        lengthened = base / "lengthened.pdf"
        invented = base / "invented.pdf"
        _write_attestation(clean, "12/03/2024", "12/03/2027", "H0B0 B1V")  # 3y, valid
        _write_attestation(lengthened, "12/03/2024", "12/03/2029", "H0B0 B1V")  # 5y!
        _write_attestation(invented, "12/03/2024", "12/03/2027", "H0B0 B9Z")  # bad code

        print("=== route_document (single doc) ===")
        clean_routed = _route(clean)
        print(f"  clean:      verdict={clean_routed.verdict.value}")
        _check(
            "clean attestation -> verdict auto", clean_routed.verdict.value == "auto"
        )

        lengthened_routed = _route(lengthened)
        print(
            f"  lengthened: verdict={lengthened_routed.verdict.value} "
            f"reasons={lengthened_routed.reasons}"
        )
        _check(
            "pen-lengthened validity -> verdict reject",
            lengthened_routed.verdict.value == "reject",
        )
        invented_routed = _route(invented)
        print(f"  invented:   verdict={invented_routed.verdict.value}")
        _check(
            "invented code -> verdict reject", invented_routed.verdict.value == "reject"
        )

        print("\n=== process_batch (the auto/review/reject split) ===")
        engine = None  # born-digital: text layer, no OCR engine needed
        result = process_batch(
            [
                BatchItem(paths=[clean], document_type="attestation"),
                BatchItem(paths=[lengthened], document_type="attestation"),
                BatchItem(paths=[invented], document_type="attestation"),
            ],
            templates_directory=Path("."),
            engine=engine,  # type: ignore[arg-type]
            templates=[_ATTESTATION_TEMPLATE],
            today=_TODAY,
        )
        print(
            f"  auto={len(result.auto)} review={len(result.review)} "
            f"rejected={len(result.rejected)}"
        )
        _check(
            "1 auto, 0 review, 2 rejected",
            len(result.auto) == 1
            and len(result.review) == 0
            and len(result.rejected) == 2,
        )
        _check(
            "both rejected records carry outcome 'reject'",
            all(record.outcome == "reject" for record in result.rejected),
        )

        print("\n=== sink bridge (_job_from_record) ===")
        clean_record: DocumentRecord = result.auto[0]
        rejected_record: DocumentRecord = result.rejected[0]
        _check(
            "auto record -> D1 status 'done' / verdict 'auto'",
            (job := _job_from_record(clean_record)).status == STATUS_DONE
            and job.verdict == "auto",
        )
        _check(
            "reject record -> D1 status 'rejected' / verdict 'reject' (terminal, no human)",
            (job := _job_from_record(rejected_record)).status == STATUS_REJECTED
            and job.verdict == "reject",
        )

        print(f"\nVERDICT FLOW SMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
