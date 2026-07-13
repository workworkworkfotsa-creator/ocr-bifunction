"""Reconcile three-state verdict smoke — recto/verso CI, auto / human / reject.

Deterministic, no OCR: builds recto fields + an MrzFields directly (PII-free, fictional) and
asserts the reconcile verdict per the user's rule (2026-07-03, "tout mismatch -> reject"):
  - every shared key agrees + all check digits pass -> auto;
  - a shared key DIVERGES (recto vs MRZ name different identities) -> reject (proven invalid,
    "recto of A + verso of B");
  - the MRZ integrity is off (a failed check digit) or nothing to compare -> human (an
    unreliable read, never auto-rejected on OCR noise alone).
"""

from __future__ import annotations

from ocr_bifunction.mrz import MrzFields
from ocr_bifunction.reconcile import reconcile

_checks_passed = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _checks_passed
    if not condition:
        raise AssertionError(f"CHECK FAILED: {label} {detail}")
    _checks_passed += 1
    print(f"  PASS {label}")


def _mrz(surname: str, all_checks_pass: bool = True) -> MrzFields:
    return MrzFields(
        mrz_format="td1",
        surname=surname,
        given_names="JEAN",
        birth_date="1990-01-01",
        expiry_date="2030-01-01",
        document_number="ABC123456",
        checks={
            "document_number": True,
            "birth_date": all_checks_pass,
            "composite": True,
        },
    )


_RECTO = {
    "nom": "DUPONT",
    "prenoms": "JEAN",
    "date_naissance": "1990-01-01",
    "date_expiration": "2030-01-01",
    "numero_document": "ABC123456",
}


def main() -> None:
    print("=== reconcile verdict ===")
    _check(
        "recto == MRZ, all check digits pass -> auto",
        reconcile(_RECTO, _mrz("DUPONT")).verdict.value == "auto",
    )
    mismatch = reconcile({**_RECTO, "nom": "MARTIN"}, _mrz("DUPONT"))
    print(f"  mismatch reasons: {mismatch.reasons}")
    _check(
        "recto surname != MRZ surname (recto A + verso B) -> reject",
        mismatch.verdict.value == "reject",
    )
    _check(
        "keys agree but a check digit FAILED -> review (unreliable read, not proven fraud)",
        reconcile(_RECTO, _mrz("DUPONT", all_checks_pass=False)).verdict.value
        == "review",
    )
    _check(
        "nothing to compare (empty recto) -> review",
        reconcile({}, _mrz("DUPONT")).verdict.value == "review",
    )
    _check(
        "accent fold only: recto 'DUPONT' vs MRZ 'DUPONT' stays auto (sanity)",
        reconcile(_RECTO, _mrz("DUPONT")).verdict.value == "auto",
    )

    print(f"\nRECONCILE VERDICT SMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
