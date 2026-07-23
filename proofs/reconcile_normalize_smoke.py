"""Logic oracle for the reconcile accent-folding debt (_normalize).

    uv run python proofs/reconcile_normalize_smoke.py

The real corpus card that exercises the combined-PDF flow (AIT-ALLA) has no accent, so it
cannot prove the fix. This checks the accent-folding directly: an accented recto value now
compares equal to the ICAO-transliterated (accent-free) MRZ, WITHOUT loosening exact match
— a genuine one-character difference (the kind a VLM slip produces) still mismatches. That
last case is the evidence for the deferred fuzzy-name decision: tolerance there is a
security trade-off, not folded in for free here.
"""

from __future__ import annotations

from ocr_bifunction.identity_key import strict_identity_key as _normalize

# (recto_value, mrz_value, should_match, label)
CASES: list[tuple[str, str, bool, str]] = [
    (
        "Maelys-Gaëlle",
        "MAELYS GAELLE",
        True,
        "accented vs ICAO-folded -> match (the debt fix)",
    ),
    ("Gaëlle", "GAELLE", True, "single accent folded -> match"),
    ("AIT-ALLA", "AIT ALLA", True, "punctuation/space only -> match"),
    ("Mostafa", "MOSTAFA", True, "case only -> match"),
    ("Gaële", "GAELE", True, "accent folded, same letters -> match"),
    (
        "Gaëlle",
        "GAELE",
        False,
        "genuine 1-char diff -> STILL mismatch (no fuzzy tolerance)",
    ),
    ("MARTIN", "BERNARD", False, "different surname -> mismatch"),
]


def main() -> int:
    all_passed = True
    for recto_value, mrz_value, should_match, label in CASES:
        matched = _normalize(recto_value) == _normalize(mrz_value)
        ok = matched == should_match
        all_passed &= ok
        outcome = "match" if matched else "mismatch"
        print(f"[{'PASS' if ok else 'FAIL'}] {label:54} -> {outcome}")
    print("=" * 72)
    print("ALL PASS" if all_passed else "SOME FAILED")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
