"""Escalation terminal-state smoke — the closed reject-drop hole (Verdict candidate A).

Before the Verdict value object, `worker_watchdog._terminal_from_record` mapped `auto -> done`
and EVERYTHING ELSE -> `needs_review/human`, silently collapsing an escalated CI `reject` (a
recto/MRZ identity mismatch surfaced by the heavier read) into human review — contradicting the
routed table right beside it (`reject -> rejected`). The door catches a CI reject before it can
reach escalation, so the branch was unreachable in the live flow and the bug stayed latent.

This proves the bridge itself now honours all three states via the shared `Verdict.d1_status`,
so the hole cannot reopen. DB-free and image-free: it drives the exact function that was wrong.
"""

from __future__ import annotations

from ocr_bifunction.pipeline import CiRecord
from ocr_bifunction.status import STATUS_DONE, STATUS_NEEDS_REVIEW, STATUS_REJECTED
from ocr_bifunction.verdict import Verdict
from worker_watchdog import _terminal_from_record

_checks_passed = 0


def _check(label: str, condition: bool) -> None:
    global _checks_passed
    print(f"  {'PASS' if condition else 'FAIL'} {label}")
    assert condition, label
    _checks_passed += 1


def _record(verdict: Verdict) -> CiRecord:
    return CiRecord(
        fields={"nom": "DUPONT"},
        verdict=verdict,
        reasons=["mismatch"] if verdict is Verdict.REJECT else [],
        verso_read_path="escalation",
    )


def main() -> None:
    print("=== escalation terminal state (worker bridge) ===")

    status_value, verdict_value, fields, reasons = _terminal_from_record(
        _record(Verdict.REJECT)
    )
    _check(
        "escalated CI reject -> STATUS_REJECTED (the closed hole, no longer needs_review)",
        status_value == STATUS_REJECTED and verdict_value == "reject",
    )
    _check(
        "the reject reasons + verso-read provenance survive",
        fields == {"nom": "DUPONT"}
        and reasons == ["mismatch", "verso read via: escalation"],
    )

    status_value, verdict_value, _, _ = _terminal_from_record(_record(Verdict.AUTO))
    _check(
        "escalated CI auto -> STATUS_DONE (iso-output)",
        status_value == STATUS_DONE and verdict_value == "auto",
    )

    status_value, verdict_value, _, _ = _terminal_from_record(_record(Verdict.REVIEW))
    _check(
        "escalated CI review -> STATUS_NEEDS_REVIEW (iso-output)",
        status_value == STATUS_NEEDS_REVIEW and verdict_value == "review",
    )

    status_value, verdict_value, fields, _ = _terminal_from_record(None)
    _check(
        "no record from escalation -> needs_review, no verdict",
        status_value == STATUS_NEEDS_REVIEW
        and verdict_value is None
        and fields is None,
    )

    print(f"\nESCALATION REJECT SMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
