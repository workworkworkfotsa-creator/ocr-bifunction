"""Logic oracle for the invoice value-check (HT + TVA = TTC).

    uv run python facture_validation_smoke.py

The real corpus is entirely zero-VAT (autoliquidation / 293 B), so on real data the sum
check only ever proves HT + 0 = HT — it never DISCRIMINATES a non-zero VAT nor CATCHES a
wrong total. A value-check is worth nothing if it cannot catch a mismatch, so this smoke
exercises `validate_fields` directly on synthetic amounts the corpus can't supply: a
correct non-zero VAT passes, a wrong total fails, the tolerance band holds, comma decimals
parse, and a missing amount fails loud. No PII, no I/O — pure logic on the shared evaluator.
"""

from __future__ import annotations

from ocr_bifunction.template import validate_fields

# Mirror of a full-VAT template's validation block (e.g. facture_sortante_01).
SUM_VALIDATION = {
    "required": [
        {"field": "montant_ht", "check": "present"},
        {"field": "montant_ttc", "check": "present"},
        {
            "check": "sum",
            "terms": ["montant_ht", "montant_tva"],
            "equals": "montant_ttc",
            "tolerance": 0.01,
        },
    ]
}

# (label, fields, expect_pass) — expect_pass True means "auto" (no reasons).
CASES: list[tuple[str, dict[str, str | None], bool]] = [
    (
        "non-zero VAT, correct",
        {"montant_ht": "1000.00", "montant_tva": "200.00", "montant_ttc": "1200.00"},
        True,
    ),
    (
        "non-zero VAT, wrong total",
        {"montant_ht": "1000.00", "montant_tva": "200.00", "montant_ttc": "1300.00"},
        False,
    ),
    (
        "within tolerance (0.01)",
        {"montant_ht": "100.00", "montant_tva": "33.33", "montant_ttc": "133.34"},
        True,
    ),
    (
        "outside tolerance",
        {"montant_ht": "100.00", "montant_tva": "33.33", "montant_ttc": "133.40"},
        False,
    ),
    (
        "comma decimals, correct",
        {"montant_ht": "1234,50", "montant_tva": "246,90", "montant_ttc": "1481,40"},
        True,
    ),
    (
        "missing TTC (fail loud)",
        {"montant_ht": "1000.00", "montant_tva": "200.00", "montant_ttc": None},
        False,
    ),
    (
        "unparseable amount",
        {"montant_ht": "1000.00", "montant_tva": "n/a", "montant_ttc": "1200.00"},
        False,
    ),
]


def main() -> int:
    all_passed = True
    for label, fields, expect_pass in CASES:
        reasons = validate_fields(fields, SUM_VALIDATION)
        got_pass = not reasons
        ok = got_pass == expect_pass
        all_passed &= ok
        verdict = "auto" if got_pass else "human"
        print(f"[{'PASS' if ok else 'FAIL'}] {label:28} -> {verdict}")
        for reason in reasons:
            print(f"            - {reason}")
    print("=" * 56)
    print("ALL PASS" if all_passed else "SOME FAILED")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
