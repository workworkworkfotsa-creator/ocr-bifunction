"""Handler smoke — intake.handle_document in isolation, on the in-memory Store (Candidate B).

Proves the shared document-handling layer WITHOUT FastAPI or a subprocess: each verdict maps to
the right outcome, the non-conformity reaction obeys the conformity policy (block vs
flag_and_continue), a declared-vs-recognized type mismatch is caught, and job_from_outcome +
an in-memory SqliteRepository round-trip the persisted row. Born-digital synthetic attestations
(PII-free), engine=None, Store(':memory:') — milliseconds. This is the step-1 gate: the handler
is proven before either entry point is cut over to it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from draft_smoke import _write_pdf
from ocr_bifunction.conformity_policy import (
    CONFORMITY_ACTION_FLAG_AND_CONTINUE,
    ConformityPolicy,
)
from ocr_bifunction.intake import handle_document, job_from_outcome
from ocr_bifunction.orchestrator import BatchItem
from ocr_bifunction.repository import SqliteRepository
from ocr_bifunction.store import Store
from verdict_flow_check import _ATTESTATION_TEMPLATE, _TODAY, _write_attestation

_TEMPLATES = [_ATTESTATION_TEMPLATE]
_checks_passed = 0


def _check(label: str, condition: bool) -> None:
    global _checks_passed
    print(f"  {'PASS' if condition else 'FAIL'} {label}")
    assert condition, label
    _checks_passed += 1


def _handle(item: BatchItem, conformity_policies=None):
    return handle_document(
        item,
        templates_directory=Path("."),  # unused on the routed lane (templates injected)
        engine=None,  # born-digital: text layer, no OCR engine needed
        templates=_TEMPLATES,
        today=_TODAY,
        conformity_policies=conformity_policies or {},
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        clean = base / "clean.pdf"
        lengthened = base / "lengthened.pdf"
        memo = base / "memo.pdf"
        _write_attestation(clean, "12/03/2024", "12/03/2027", "H0B0 B1V")  # valid 3y
        _write_attestation(lengthened, "12/03/2024", "12/03/2029", "H0B0 B1V")  # 5y!
        _write_pdf(
            memo,
            [
                (72, 70, "Note interne"),
                (72, 100, "Rien de structure ici, juste du texte libre"),
                (72, 130, "aucun template ne matche ce document"),
            ],
        )

        print("=== handle_document: verdict -> outcome ===")
        auto = _handle(BatchItem(paths=[clean], document_type="attestation"))
        _check(
            "clean attestation -> done/auto, no retention",
            auto.status == "done"
            and auto.verdict == "auto"
            and not auto.retain_bytes
            and not auto.nonconformity,
        )

        memo_outcome = _handle(BatchItem(paths=[memo], document_type=None))
        _check(
            "unmatched doc (RAG) -> needs_review, retained, no non-conformity",
            memo_outcome.status == "needs_review"
            and memo_outcome.verdict == "review"
            and memo_outcome.retain_bytes
            and not memo_outcome.nonconformity,
        )

        print("\n=== non-conformity reaction (conformity policy) ===")
        blocked = _handle(BatchItem(paths=[lengthened], document_type="attestation"))
        _check(
            "reject verdict + default policy (block) -> rejected non-conformity, retained",
            blocked.status == "rejected"
            and blocked.verdict == "reject"
            and blocked.nonconformity
            and blocked.retain_bytes,
        )

        flagged = _handle(
            BatchItem(paths=[lengthened], document_type="attestation"),
            conformity_policies={
                "attestation": ConformityPolicy(
                    category="attestation", action=CONFORMITY_ACTION_FLAG_AND_CONTINUE
                )
            },
        )
        _check(
            "reject verdict + flag_and_continue -> needs_review, flagged",
            flagged.status == "needs_review"
            and flagged.verdict == "review"
            and flagged.nonconformity
            and any("FLAGGED" in reason for reason in flagged.reasons),
        )

        print("\n=== type mismatch (declared != recognized) ===")
        mismatch = _handle(BatchItem(paths=[clean], document_type="facture"))
        _check(
            "attestation declared as facture -> type-mismatch non-conformity (rejected)",
            mismatch.status == "rejected"
            and mismatch.nonconformity
            and mismatch.record.category == "attestation"
            and any("type mismatch" in reason for reason in mismatch.reasons),
        )

        print("\n=== job_from_outcome + in-memory Store round-trip ===")
        store = Store(":memory:")
        repository = SqliteRepository(store)
        job = job_from_outcome(
            blocked,
            source="real_name.pdf",
            request_id="req-1",
            document_ref="/spool/req-1" if blocked.retain_bytes else None,
            expected_holder_name="FICTIF Alice",
        )
        job_id = repository.save(job)
        stored = repository.get(job_id)
        _check(
            "job_from_outcome -> saved row round-trips (source override, status, verdict, ref)",
            stored is not None
            and stored.source == "real_name.pdf"
            and stored.status == "rejected"
            and stored.verdict == "reject"
            and stored.document_ref == "/spool/req-1"
            and stored.expected_holder_name == "FICTIF Alice",
        )
        store.close()

    print(f"\nHANDLER SMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
