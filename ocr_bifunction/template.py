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


def load_templates(directory: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]


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
    anchor_x0, anchor_y0 = anchor_line.bbox[0], anchor_line.bbox[1]
    candidates = [
        line
        for line in lines
        if line is not anchor_line
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
    if rule == "upper":
        return value.upper()
    return value


def extract_fields(lines: list[TextLine], template: dict) -> dict[str, str | None]:
    """Rebuild the template's named fields from the line geometry."""
    extracted: dict[str, str | None] = {}
    for field in template["fields"]:
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
