"""Stage ②③ — match a category template and rebuild structured fields from geometry.

Raw OCR lines carry no links; their boxes do. A template names, per field, a label
anchor and the spatial rule that ties it to its value ("the value sits below the
label, in the same column"). This is the deterministic Python post-processing the
Backoffice validates — no model, just geometry + rules.

Several templates can exist per category (a CI has many formats); match_template
picks the one whose signature anchors are all present.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from ocr_bifunction.reader import TextLine

# Horizontal tolerance (pixels) for "same column": a value counts as below a label
# when their left edges line up within this band. Tuned on ~1100px-wide CI scans.
COLUMN_X_TOLERANCE = 60.0
# Vertical tolerance (pixels) for "same row" (direction "right").
ROW_Y_TOLERANCE = 25.0


def load_templates(directory: Path, category: str | None = None) -> list[dict]:
    """Load every template JSON, optionally keeping only one document category.

    `category` is the optional document-type hint (e.g. "carte_identite"): when the
    caller already knows the upload is a CI, only CI templates are considered — an
    invoice template can never accidentally match, and matching is cheaper. None loads
    every template (the default, category-agnostic behavior).
    """
    templates = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]
    if category is not None:
        templates = [
            template for template in templates if template.get("category") == category
        ]
    return templates


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _fuzzy_contains(needle: str, haystack: str, threshold: float = 0.75) -> bool:
    """True if `needle` appears in `haystack`, tolerant of OCR slips (e.g. rn->m).

    Real cards break exact anchors: "Surname" is read "Sumame". We slide a window
    of ~len(needle) over the line and accept a close enough match.
    """
    if not needle:
        return False
    if needle in haystack:
        return True
    if len(needle) < 4:  # too short to fuzzy-match without false positives
        return False
    for window in (len(needle) - 1, len(needle), len(needle) + 1):
        for start in range(len(haystack) - window + 1):
            candidate = haystack[start : start + window]
            if difflib.SequenceMatcher(None, needle, candidate).ratio() >= threshold:
                return True
    return False


def _find_anchor_line(lines: list[TextLine], anchor: str) -> TextLine | None:
    needle = _normalize_for_match(anchor)
    for line in lines:
        if _fuzzy_contains(needle, _normalize_for_match(line.text)):
            return line
    return None


def match_template(lines: list[TextLine], templates: list[dict]) -> dict | None:
    """Return the first template whose signature anchors are all found in `lines`."""
    for template in templates:
        required_anchors = template.get("match", {}).get("all_anchors", [])
        if required_anchors and all(
            _find_anchor_line(lines, anchor) for anchor in required_anchors
        ):
            return template
    return None


def _value_below(lines: list[TextLine], anchor_line: TextLine) -> TextLine | None:
    # Same page only: bbox coordinates are per-page, so a cross-page comparison is
    # meaningless (seen on 2-page attestations: a p1 block "below" a p0 label).
    anchor_x0, anchor_y0 = anchor_line.bbox[0], anchor_line.bbox[1]
    candidates = [
        line
        for line in lines
        if line is not anchor_line
        and line.page_index == anchor_line.page_index
        and line.bbox[1] > anchor_y0 + 5
        and abs(line.bbox[0] - anchor_x0) <= COLUMN_X_TOLERANCE
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda line: line.bbox[1] - anchor_y0)


def _value_right(lines: list[TextLine], anchor_line: TextLine) -> TextLine | None:
    anchor_x1, anchor_y0 = anchor_line.bbox[2], anchor_line.bbox[1]
    candidates = [
        line
        for line in lines
        if line is not anchor_line
        and line.page_index == anchor_line.page_index
        and line.bbox[0] >= anchor_x1 - 5
        and abs(line.bbox[1] - anchor_y0) <= ROW_Y_TOLERANCE
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda line: line.bbox[0] - anchor_x1)


def _normalize_value(value: str, rule: str) -> str:
    value = value.strip()
    if rule == "date_ddmmyyyy":
        digits = re.sub(r"\D", "", value)
        if len(digits) == 8:
            return f"{digits[4:]}-{digits[2:4]}-{digits[0:2]}"  # DDMMYYYY -> ISO
    if rule == "amount":
        # French thousands separators (space / NBSP / narrow NBSP) -> bare number.
        return re.sub(r"[\s  ]", "", value)
    if rule == "upper":
        return value.upper()
    return value


def _extract_by_pattern(document_text: str, field: dict) -> str | None:
    """Extract a field by regex over the document text (group 1, else the whole match).

    Born-digital PDFs glue a label to its value inside one PyMuPDF block, so geometry
    anchors do not apply — these fields name a regex instead.
    """
    match = re.search(field["pattern"], document_text)
    if match is None:
        return None
    value = match.group(1) if match.groups() else match.group(0)
    return _normalize_value(value, field.get("normalize", "strip"))


def extract_fields(lines: list[TextLine], template: dict) -> dict[str, str | None]:
    """Rebuild the template's named fields, by geometry anchors OR by text patterns.

    A field with a `pattern` key is extracted by regex over the document text (born-digital
    invoices, where PyMuPDF glues label+value in one block). A field with an `anchor` key
    uses the geometry path (scanned cards). A template may mix both.
    """
    document_text = "\n".join(line.text for line in lines)
    extracted: dict[str, str | None] = {}
    for field in template["fields"]:
        if "pattern" in field:
            extracted[field["name"]] = _extract_by_pattern(document_text, field)
            continue
        anchor_line = _find_anchor_line(lines, field["anchor"])
        if anchor_line is None:
            extracted[field["name"]] = None
            continue
        direction = field.get("direction", "below")
        if direction == "right":
            value_line = _value_right(lines, anchor_line)
        else:
            value_line = _value_below(lines, anchor_line)
        if value_line is None:
            extracted[field["name"]] = None
            continue
        extracted[field["name"]] = _normalize_value(
            value_line.text, field.get("normalize", "strip")
        )
    return extracted


def _parse_amount(value: str | None) -> float | None:
    """Parse a normalized amount ('1234,56' or '1234.56') to a float, or None.

    The "amount" normalize already stripped thousands separators (space / NBSP); only a
    comma-or-dot decimal remains. Returns None on an empty or unparseable value so the
    caller fails loud with a reason instead of silently passing a bad check.
    """
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def _check_sum(fields: dict[str, str | None], rule: dict) -> list[str]:
    """Value check: the `terms` amounts must sum to the `equals` amount within tolerance.

    e.g. montant_ht + montant_tva == montant_ttc. A missing/unparseable input fails loud
    (the check cannot be proven, so it is NOT a silent pass).
    """
    terms: list[str] = rule["terms"]
    equals_field: str = rule["equals"]
    tolerance: float = rule.get("tolerance", 0.01)

    parsed_terms = {name: _parse_amount(fields.get(name)) for name in terms}
    total = _parse_amount(fields.get(equals_field))
    missing = [name for name, amount in parsed_terms.items() if amount is None]
    if total is None:
        missing.append(equals_field)
    if missing:
        return [f"sum check ({equals_field}): missing/unreadable {', '.join(missing)}"]

    # Compare in integer cents: amounts are 2-decimal currency, and float subtraction
    # leaves noise (100.00 + 33.33 - 133.34 != exactly -0.01) that would tip a knife-edge
    # one-cent tolerance the wrong way. Cents make the money comparison exact.
    summed = sum(amount for amount in parsed_terms.values() if amount is not None)
    difference_cents = abs(round(summed * 100) - round(total * 100))
    if difference_cents > round(tolerance * 100):
        return [
            f"sum check failed: {' + '.join(terms)} = {summed:.2f} "
            f"!= {equals_field} = {total:.2f} (tolerance {tolerance})"
        ]
    return []


def validate_fields(fields: dict[str, str | None], validation: dict) -> list[str]:
    """Evaluate a template's `validation` block against extracted fields -> failure reasons.

    Empty list = every required check passed (-> auto). Config-driven: the checks travel
    WITH the template (the Backoffice curates them), so the SAME evaluator serves every
    document type. Two check kinds, both declared per template:
      - present: the named field must be non-empty (presence, value-agnostic).
      - sum:     `terms` must sum to `equals` within `tolerance` (a value check).
    A layout with no cross-check to make (e.g. TVA autoliquidation) simply declares fewer
    rules — the absence of a sum check is the template's honest statement, not a gap.
    """
    reasons: list[str] = []
    for rule in validation.get("required", []):
        check = rule.get("check")
        if check == "present":
            field_name = rule["field"]
            if not fields.get(field_name):
                reasons.append(f"{field_name}: required (presence) but not read")
        elif check == "sum":
            reasons.extend(_check_sum(fields, rule))
        else:
            reasons.append(f"unknown validation check: {check!r}")
    return reasons
