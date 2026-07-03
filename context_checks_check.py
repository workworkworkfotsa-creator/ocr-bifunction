"""Context-dependent anti-fraud check smoke — reconcile_ci, issuer_registry, corroborated_by.

Deterministic, no machine, no OCR: exercises validate_fields with a ValidationContext on
synthetic fields (PII-free, obviously fictional). Proves each context check:
  1. PASSES a legitimate document (name on file, issuer registered, titre corroborated);
  2. PULLS its fraud (the sibling's close name, a home-made issuer, an uncorroborated
     self-declared titre — "ma mere peut me faire une certif");
  3. FAILS LOUD when its context is absent (never a silent pass).

The two issuer REGIMES are shown end to end: attestation_formation (organism-issued ->
issuer_registry) auto-validates on its own; titre_habilitation (employer self-declared ->
reconcile_ci + corroborated_by) never does without a validated attestation behind it.
"""

from __future__ import annotations

from ocr_bifunction.template import (
    AttestationReference,
    ValidationContext,
    validate_fields,
)

_checks_passed = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _checks_passed
    if not condition:
        raise AssertionError(f"CHECK FAILED: {label} {detail}")
    _checks_passed += 1
    print(f"  PASS {label}")


def _validate(
    fields: dict[str, str | None],
    required: list[dict],
    context: ValidationContext | None = None,
) -> list[str]:
    return validate_fields(fields, {"required": required}, context=context)


def main() -> None:
    print("=== reconcile_ci (holder strictly matches the CI record) ===")
    reconcile_rule = [{"check": "reconcile_ci", "field": "name_of_holder"}]
    _check(
        "same name passes",
        _validate(
            {"name_of_holder": "FICTIF Ahmed"},
            reconcile_rule,
            ValidationContext(ci_reference_name="FICTIF Ahmed"),
        )
        == [],
    )
    _check(
        "accent folding only (Gaelle == Gaëlle) passes",
        _validate(
            {"name_of_holder": "FICTIF Gaelle"},
            reconcile_rule,
            ValidationContext(ci_reference_name="FICTIF Gaëlle"),
        )
        == [],
    )
    _check(
        "sibling fraud: Hamed does NOT match Ahmed (strict) -> fails",
        _validate(
            {"name_of_holder": "FICTIF Hamed"},
            reconcile_rule,
            ValidationContext(ci_reference_name="FICTIF Ahmed"),
        )
        != [],
    )
    _check(
        "no CI reference in context -> fails loud",
        _validate({"name_of_holder": "FICTIF Ahmed"}, reconcile_rule, None) != [],
    )

    print("\n=== issuer_registry (issuer in the recognized-organism registry) ===")
    registry_rule = [{"check": "issuer_registry", "field": "issuer_siret"}]
    registry = ValidationContext(
        issuer_registry=frozenset({"123 456 789 00012", "987 654 321 00034"})
    )
    _check(
        "registered SIRET (spacing-insensitive) passes",
        _validate({"issuer_siret": "12345678900012"}, registry_rule, registry) == [],
    )
    _check(
        "home-made issuer (unknown SIRET) -> fails",
        _validate({"issuer_siret": "00000000000000"}, registry_rule, registry) != [],
    )
    _check(
        "no registry in context -> fails loud",
        _validate({"issuer_siret": "12345678900012"}, registry_rule, None) != [],
    )

    print(
        "\n=== corroborated_by (self-declared titre backed by a validated attestation) ==="
    )
    corroborate_rule = [
        {
            "check": "corroborated_by",
            "holder_field": "name_of_holder",
            "issue_field": "date_of_issue",
        }
    ]
    on_file = ValidationContext(
        validated_attestations=[
            AttestationReference("FICTIF Ahmed", "2024-01-10", "2027-01-10"),
            AttestationReference("EXEMPLE Bruno", "2023-05-01", "2026-05-01"),
        ]
    )
    _check(
        "titre within a matching attestation's validity window passes",
        _validate(
            {"name_of_holder": "FICTIF Ahmed", "date_of_issue": "2024-03-12"},
            corroborate_rule,
            on_file,
        )
        == [],
    )
    _check(
        '"ma mere": no attestation for this holder -> fails',
        _validate(
            {"name_of_holder": "INCONNU Sophie", "date_of_issue": "2024-03-12"},
            corroborate_rule,
            on_file,
        )
        != [],
    )
    _check(
        "titre issued OUTSIDE the attestation window (before training) -> fails",
        _validate(
            {"name_of_holder": "FICTIF Ahmed", "date_of_issue": "2023-01-01"},
            corroborate_rule,
            on_file,
        )
        != [],
    )
    _check(
        "no attestations in context -> fails loud",
        _validate(
            {"name_of_holder": "FICTIF Ahmed", "date_of_issue": "2024-03-12"},
            corroborate_rule,
            None,
        )
        != [],
    )

    print("\n=== the two issuer regimes, end to end ===")
    # attestation_formation: organism-issued -> issuer_registry is its own strong proof.
    attestation_required = [
        {"field": "name_of_holder", "check": "present"},
        {"check": "issuer_registry", "field": "issuer_siret"},
    ]
    _check(
        "attestation_formation from a registered organism auto-validates",
        _validate(
            {"name_of_holder": "FICTIF Ahmed", "issuer_siret": "12345678900012"},
            attestation_required,
            registry,
        )
        == [],
    )
    # titre_habilitation: employer self-declared -> needs the CI match AND corroboration.
    titre_required = [
        {"check": "reconcile_ci", "field": "name_of_holder"},
        *corroborate_rule,
    ]
    titre_context = ValidationContext(
        ci_reference_name="FICTIF Ahmed",
        validated_attestations=on_file.validated_attestations,
    )
    _check(
        "titre_habilitation corroborated by a validated attestation auto-validates",
        _validate(
            {"name_of_holder": "FICTIF Ahmed", "date_of_issue": "2024-03-12"},
            titre_required,
            titre_context,
        )
        == [],
    )
    _check(
        "the SAME titre without any attestation on file -> needs review",
        _validate(
            {"name_of_holder": "FICTIF Ahmed", "date_of_issue": "2024-03-12"},
            titre_required,
            ValidationContext(ci_reference_name="FICTIF Ahmed"),
        )
        != [],
    )

    print(f"\nCONTEXT CHECK SMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
