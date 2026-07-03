"""Three-state verdict smoke — auto / review / reject, classified from the checks.

Deterministic, no machine. Proves evaluate_validation routes each failing check to the
outcome the user confirmed (2026-07-03):
  - proven invalid (date_order / date_span / vocabulary / reconcile_ci) -> REJECT (terminal);
  - unknown / pending (present, issuer_registry, corroborated_by) -> REVIEW (human);
  - all pass -> AUTO;
  - reject BEATS review when both are present (a proven-invalid document is not softened).
"""

from __future__ import annotations

from datetime import date

from ocr_bifunction.template import (
    AttestationReference,
    ValidationContext,
    evaluate_validation,
)

_TODAY = date(2026, 7, 3)
_checks_passed = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _checks_passed
    if not condition:
        raise AssertionError(f"CHECK FAILED: {label} {detail}")
    _checks_passed += 1
    print(f"  PASS {label}")


def _verdict(
    fields: dict[str, str | None],
    required: list[dict],
    context: ValidationContext | None = None,
) -> str:
    return evaluate_validation(
        fields, {"required": required}, today=_TODAY, context=context
    ).verdict


def main() -> None:
    clean_dates = {"date_of_issue": "2024-03-12", "valid_until_date": "2027-03-12"}
    span_rule = {
        "check": "date_span",
        "start": "date_of_issue",
        "end": "valid_until_date",
        "years": 3,
    }
    vocabulary_rule = {
        "check": "vocabulary",
        "field": "codes_obtained",
        "allowed": ["H0B0", "B1V"],
    }
    reconcile_rule = {"check": "reconcile_ci", "field": "name_of_holder"}
    issuer_rule = {"check": "issuer_registry", "field": "issuer_siret"}
    corroborate_rule = {
        "check": "corroborated_by",
        "holder_field": "name_of_holder",
        "issue_field": "date_of_issue",
    }
    registry = ValidationContext(issuer_registry=frozenset({"12345678900012"}))
    ci_ahmed = ValidationContext(ci_reference_name="FICTIF Ahmed")
    attestations = ValidationContext(
        validated_attestations=[
            AttestationReference("FICTIF Ahmed", "2024-01-10", "2027-01-10")
        ]
    )

    print("=== all pass -> AUTO ===")
    _check(
        "clean span validates auto",
        _verdict(clean_dates, [span_rule]) == "auto",
    )

    print("\n=== proven invalid -> REJECT (terminal) ===")
    _check(
        "date_span broken (pen-lengthened) -> reject",
        _verdict(
            {"date_of_issue": "2024-03-12", "valid_until_date": "2029-03-12"},
            [span_rule],
        )
        == "reject",
    )
    _check(
        "invented code -> reject",
        _verdict({"codes_obtained": "H0B0 B9Z"}, [vocabulary_rule]) == "reject",
    )
    _check(
        "recto/verso holder mismatch (reconcile_ci) -> reject",
        _verdict({"name_of_holder": "FICTIF Hamed"}, [reconcile_rule], ci_ahmed)
        == "reject",
    )

    print("\n=== unknown / pending -> REVIEW (human) ===")
    _check(
        "missing required field (present) -> review",
        _verdict({}, [{"field": "name_of_holder", "check": "present"}]) == "review",
    )
    _check(
        "unregistered issuer -> review (maybe a new legit organism)",
        _verdict({"issuer_siret": "00000000000000"}, [issuer_rule], registry)
        == "review",
    )
    _check(
        "uncorroborated self-declared titre -> review (pending attestation)",
        _verdict(
            {"name_of_holder": "INCONNU Sophie", "date_of_issue": "2024-03-12"},
            [corroborate_rule],
            attestations,
        )
        == "review",
    )

    print("\n=== missing/undetermined input -> REVIEW, never REJECT ===")
    _check(
        "date_span with an unreadable date -> review (can't tell, not a forgery)",
        _verdict({"date_of_issue": "2024-03-12"}, [span_rule]) == "review",
    )
    _check(
        "reconcile_ci with NO CI context -> review (an unwired context must not reject)",
        _verdict({"name_of_holder": "FICTIF Ahmed"}, [reconcile_rule], None)
        == "review",
    )
    _check(
        "vocabulary with a missing field -> review (unread, not an invented code)",
        _verdict({}, [vocabulary_rule]) == "review",
    )

    print("\n=== reject BEATS review when both fire ===")
    both_context = ValidationContext(
        issuer_registry=frozenset({"12345678900012"}),
    )
    outcome = evaluate_validation(
        {
            "date_of_issue": "2024-03-12",
            "valid_until_date": "2029-03-12",  # date_span -> reject
            "issuer_siret": "00000000000000",  # issuer_registry -> review
        },
        {"required": [span_rule, issuer_rule]},
        today=_TODAY,
        context=both_context,
    )
    _check(
        "a proven-invalid doc that also has a pending check -> reject",
        outcome.verdict == "reject"
        and bool(outcome.reject_reasons)
        and bool(outcome.review_reasons),
        f"(verdict={outcome.verdict}, reject={outcome.reject_reasons}, "
        f"review={outcome.review_reasons})",
    )

    print(f"\nVERDICT SMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
