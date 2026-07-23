"""Anti-fraud check kit smoke — the pure value checks (date_order, date_span, vocabulary).

Deterministic, no machine, no OCR: exercises validate_fields directly on synthetic field
dicts (PII-free, obviously fictional), with an injected `today` so the freshness side of
date_order is reproducible. Proves each new check both PASSES a clean document and PULLS
the tampering it exists to catch (a pen-lengthened validity, an invented code, a swapped
or expired window). The context-dependent checks (reconcile_ci / issuer_registry /
corroborated_by) are out of scope here — they get a context-carrying evaluator.
"""

from __future__ import annotations

from datetime import date

from ocr_bifunction.extraction.template import validate_fields

_TODAY = date(2026, 7, 3)  # injected clock — keeps require_future reproducible

_checks_passed = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _checks_passed
    if not condition:
        raise AssertionError(f"CHECK FAILED: {label} {detail}")
    _checks_passed += 1
    print(f"  PASS {label}")


def _validate(fields: dict[str, str | None], required: list[dict]) -> list[str]:
    return validate_fields(fields, {"required": required}, today=_TODAY)


def main() -> None:
    print("=== date_order (earlier < later, opt-in not-expired) ===")
    order_rule = [
        {
            "check": "date_order",
            "earlier": "date_of_issue",
            "later": "valid_until_date",
            "require_future": True,
        }
    ]
    _check(
        "clean window (issue < expiry, expiry in the future) passes",
        _validate(
            {"date_of_issue": "2024-03-12", "valid_until_date": "2027-03-12"},
            order_rule,
        )
        == [],
    )
    _check(
        "swapped window (expiry before issue) fails",
        _validate(
            {"date_of_issue": "2027-03-12", "valid_until_date": "2024-03-12"},
            order_rule,
        )
        != [],
    )
    _check(
        "expired certificate (expiry in the past) fails",
        _validate(
            {"date_of_issue": "2020-03-12", "valid_until_date": "2023-03-12"},
            order_rule,
        )
        != [],
    )
    _check(
        "unreadable date fails loud (not a silent pass)",
        _validate({"date_of_issue": "2024-03-12", "valid_until_date": None}, order_rule)
        != [],
    )

    print("\n=== date_span (end == start + N years) ===")
    span_rule = [
        {
            "check": "date_span",
            "start": "date_of_issue",
            "end": "valid_until_date",
            "years": 3,
        }
    ]
    _check(
        "exact 3-year span passes",
        _validate(
            {"date_of_issue": "2024-03-12", "valid_until_date": "2027-03-12"},
            span_rule,
        )
        == [],
    )
    _check(
        "validity lengthened by pen (3y -> 5y) fails",
        _validate(
            {"date_of_issue": "2024-03-12", "valid_until_date": "2029-03-12"},
            span_rule,
        )
        != [],
    )
    _check(
        "leap-day issue folds to Feb 28 (2024-02-29 + 3y = 2027-02-28) passes",
        _validate(
            {"date_of_issue": "2024-02-29", "valid_until_date": "2027-02-28"},
            span_rule,
        )
        == [],
    )

    print("\n=== vocabulary (tokens in a closed list) ===")
    vocabulary_rule = [
        {
            "check": "vocabulary",
            "field": "codes_obtained",
            "allowed": ["H0B0", "B1V", "B2V", "BR", "BC", "B0", "H0"],
        }
    ]
    _check(
        "all real codes pass (multi-token field)",
        _validate({"codes_obtained": "H0B0 B1V"}, vocabulary_rule) == [],
    )
    _check(
        "case-insensitive (h0b0 br) passes",
        _validate({"codes_obtained": "h0b0 br"}, vocabulary_rule) == [],
    )
    _check(
        "an invented code (B9Z) fails",
        _validate({"codes_obtained": "H0B0 B9Z"}, vocabulary_rule) != [],
    )

    print("\n=== combined template validation (present + the 3 value checks) ===")
    combined_required = [
        {"field": "name_of_holder", "check": "present"},
        *order_rule,
        *span_rule,
        *vocabulary_rule,
    ]
    clean_document = {
        "name_of_holder": "FICTIF Alice",
        "date_of_issue": "2024-03-12",
        "valid_until_date": "2027-03-12",
        "codes_obtained": "H0B0 B1V",
    }
    _check(
        "a clean attestation validates green (-> auto)",
        _validate(clean_document, combined_required) == [],
    )
    tampered_document = {**clean_document, "valid_until_date": "2029-03-12"}
    tampered_reasons = _validate(tampered_document, combined_required)
    print(f"  tampered reasons: {tampered_reasons}")
    _check(
        "the pen-lengthened validity is caught by BOTH date_span and require_future"
        " is irrelevant — at least date_span fires",
        any("date_span" in reason for reason in tampered_reasons),
    )

    print(f"\nCHECK KIT SMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
