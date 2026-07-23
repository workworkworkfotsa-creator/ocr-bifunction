"""Assemble ValidationContext data FROM the stores — the data side of D-e.

The attestation-side mapping — WHICH record fields play the holder / issue / expiry
roles for `corroborated_by` — is MÉTIER CONFIG, not code (user decision 2026-07-08:
« ceci doit être configurable par le métier »). Each attestation-regime template
declares it in an `attestation_reference_roles` block:

    "attestation_reference_roles": {
        "holder_field":      "<field name holding the holder's name>",
        "issue_date_field":  "<field name holding the ISO issue date>",
        "expiry_date_field": "<field name holding the ISO expiry date>"
    }

The reviewer ASSIGNS these roles at promotion (review page selects, written by the
validate endpoint) or curates them by hand in the template JSON — the block travels
with the template (D2), like the validation checks (compute-all/config-requires).
This module then projects the CLOSED (done) D1 jobs of those templates into the
`AttestationReference` entries `corroborated_by` compares against. A template without
the block simply contributes nothing — no code change per document type, ever.
"""

from __future__ import annotations

from ocr_bifunction.storage.repository import STATUS_DONE, Repository
from ocr_bifunction.extraction.template import AttestationReference, payload_value

ATTESTATION_REFERENCE_ROLES_KEY = "attestation_reference_roles"
REFERENCE_ROLE_FIELD_KEYS = ("holder_field", "issue_date_field", "expiry_date_field")


def _roles_by_template_id(active_templates: list[dict]) -> dict[str, dict]:
    """The templates that declared a COMPLETE roles block (all three roles named)."""
    roles_by_id: dict[str, dict] = {}
    for template in active_templates:
        roles = template.get(ATTESTATION_REFERENCE_ROLES_KEY)
        if isinstance(roles, dict) and all(
            roles.get(role_key) for role_key in REFERENCE_ROLE_FIELD_KEYS
        ):
            roles_by_id[template["template_id"]] = roles
    return roles_by_id


def collect_validated_attestations(
    repository: Repository, active_templates: list[dict]
) -> list[AttestationReference]:
    """The validated attestations ON FILE: every CLOSED (done) D1 job whose template
    declares reference roles, projected through that template's own mapping. A job
    missing one of the mapped values contributes nothing (it cannot corroborate)."""
    roles_by_id = _roles_by_template_id(active_templates)
    if not roles_by_id:
        return []
    references: list[AttestationReference] = []
    for job in repository.pending(STATUS_DONE):
        roles = roles_by_id.get(job.template_id or "")
        if roles is None:
            continue
        # D1 stores value + provenance per field; corroboration compares VALUES only.
        holder_name = payload_value(job.record_fields, roles["holder_field"])
        issue_date = payload_value(job.record_fields, roles["issue_date_field"])
        expiry_date = payload_value(job.record_fields, roles["expiry_date_field"])
        if holder_name and issue_date and expiry_date:
            references.append(
                AttestationReference(
                    holder_name=holder_name,
                    issue_date=issue_date,
                    expiry_date=expiry_date,
                )
            )
    return references
