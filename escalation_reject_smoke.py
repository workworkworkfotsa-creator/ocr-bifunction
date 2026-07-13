"""Escalation terminal-state smoke — the closed reject-drop hole (Verdict candidate A),
re-anchored on the unified intake seam (candidate B, worker cutover).

Before the Verdict value object, `worker_watchdog._terminal_from_record` mapped `auto -> done`
and EVERYTHING ELSE -> `needs_review/human`, silently collapsing an escalated CI `reject` (a
recto/MRZ identity mismatch surfaced by the heavier read) into human review — contradicting the
routed table right beside it (`reject -> rejected`).

That bespoke worker mapping is GONE: the worker now processes through `intake.handle_document`,
which builds the record via `orchestrator._record_from_ci` (the CI record -> DocumentRecord
mapping) and lands the D1 status through the shared `Verdict.d1_status`. This proves the hole
stays closed at that new seam — a reject record keeps its `reject` outcome (never softened to
review) and its verso-read provenance survives onto the record for the worker to fold into the
row's reasons ('verso read via: …'). DB-free and image-free: it drives the exact mapping code.
"""

from __future__ import annotations

from pathlib import Path

from ocr_bifunction.orchestrator import BatchItem, _record_from_ci
from ocr_bifunction.pipeline import CiRecord, CiSubmissionResult
from ocr_bifunction.status import STATUS_DONE, STATUS_NEEDS_REVIEW, STATUS_REJECTED
from ocr_bifunction.verdict import Verdict

_checks_passed = 0

_CI_ITEM = BatchItem(
    paths=[Path("recto.jpg"), Path("verso.jpg")], document_type="carte_identite"
)


def _check(label: str, condition: bool) -> None:
    global _checks_passed
    print(f"  {'PASS' if condition else 'FAIL'} {label}")
    assert condition, label
    _checks_passed += 1


def _complete_result(verdict: Verdict) -> CiSubmissionResult:
    """A complete CI submission whose escalated read carries `verdict` (verso via escalation)."""
    return CiSubmissionResult(
        status="complete",
        record=CiRecord(
            fields={"nom": "DUPONT"},
            verdict=verdict,
            reasons=["mismatch"] if verdict is Verdict.REJECT else [],
            verso_read_path="escalation",
        ),
    )


def main() -> None:
    print("=== escalated CI record -> terminal D1 state (unified intake seam) ===")

    reject = _record_from_ci(_complete_result(Verdict.REJECT), _CI_ITEM)
    _check(
        "escalated CI reject keeps its 'reject' outcome (never collapsed to review)",
        reject.outcome == "reject",
    )
    _check(
        "reject outcome maps to STATUS_REJECTED via the shared Verdict (the closed hole)",
        Verdict(reject.outcome).d1_status == STATUS_REJECTED,
    )
    _check(
        "the reject reasons + verso-read provenance survive onto the record",
        reject.reasons == ["mismatch"] and reject.verso_read_path == "escalation",
    )

    auto = _record_from_ci(_complete_result(Verdict.AUTO), _CI_ITEM)
    _check(
        "escalated CI auto -> STATUS_DONE (iso-output)",
        auto.outcome == "auto" and Verdict(auto.outcome).d1_status == STATUS_DONE,
    )

    review = _record_from_ci(_complete_result(Verdict.REVIEW), _CI_ITEM)
    _check(
        "escalated CI review -> STATUS_NEEDS_REVIEW (iso-output)",
        review.outcome == "review"
        and Verdict(review.outcome).d1_status == STATUS_NEEDS_REVIEW,
    )

    incomplete = _record_from_ci(
        CiSubmissionResult(
            status="incomplete", missing=["verso"], reasons=["no verso"]
        ),
        _CI_ITEM,
    )
    _check(
        "escalation that recognizes no CI (no record) -> review, no verso provenance",
        incomplete.outcome == "review"
        and Verdict(incomplete.outcome).d1_status == STATUS_NEEDS_REVIEW
        and incomplete.verso_read_path is None,
    )

    print(f"\nESCALATION REJECT SMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
