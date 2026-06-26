"""Stage ② cross-validation — reconcile the recto template fields against the MRZ.

The MRZ replays the recto's identity with check digits. Comparing the shared keys
turns "recto + verso" into one self-validating record: if every shared key agrees
AND the MRZ check digits pass, the document validates itself -> AUTO. Any divergence
-> HUMAN. This is what catches "recto of person A + verso of person B" — the concrete
failure mode behind ~200 desks validating blind at ~90% error.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ocr_bifunction.mrz import MrzFields

# recto template field name -> MrzFields attribute
_KEY_MAP = {
    "numero_document": "document_number",
    "nom": "surname",
    "prenoms": "given_names",
    "date_naissance": "birth_date",
    "date_expiration": "expiry_date",
}


@dataclass
class ReconcileResult:
    verdict: str  # "auto" | "human"
    key_matches: dict[str, bool]  # recto field -> recto value == mrz value
    failed_checks: list[str]  # MRZ check digits that did not pass
    reasons: list[str] = field(default_factory=list)


def _normalize(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper()) if value else ""


def reconcile(recto_fields: dict[str, str | None], mrz: MrzFields) -> ReconcileResult:
    """Compare the recto's fields against the MRZ on every shared key."""
    key_matches: dict[str, bool] = {}
    reasons: list[str] = []

    for recto_key, mrz_attribute in _KEY_MAP.items():
        recto_value = recto_fields.get(recto_key)
        mrz_value = getattr(mrz, mrz_attribute)
        if recto_value is None or mrz_value is None:
            continue  # absent on one side -> nothing to compare
        matches = _normalize(recto_value) == _normalize(mrz_value)
        key_matches[recto_key] = matches
        if not matches:
            reasons.append(f"{recto_key}: recto={recto_value!r} != mrz={mrz_value!r}")

    failed_checks = [name for name, passed in mrz.checks.items() if not passed]
    if failed_checks:
        reasons.append(f"MRZ check digits failed: {failed_checks}")

    all_keys_agree = bool(key_matches) and all(key_matches.values())
    if not key_matches:
        reasons.append("no shared key could be compared")

    verdict = "auto" if (all_keys_agree and not failed_checks) else "human"
    return ReconcileResult(
        verdict=verdict,
        key_matches=key_matches,
        failed_checks=failed_checks,
        reasons=reasons,
    )
