"""Stage ②③ — match a category template and rebuild structured fields from geometry.

Raw OCR lines carry no links; their boxes do. A template names, per field, a label
anchor and the spatial rule that ties it to its value ("the value sits below the
label, in the same column"). This is the deterministic Python post-processing the
Backoffice validates — no model, just geometry + rules.

Several templates can exist per category (a CI has many formats); match_template
picks the one whose signature anchors are all present.

This module is the EXTRACTION half. The VERDICT half — the config-driven anti-fraud checks
that classify the extracted `fields` for routing — lives in `validation.py`; its public names
(`ValidationContext`, `evaluate_validation`, `validate_fields`, `CheckFailure`, …) are
re-exported below so existing importers keep one import site while the seam is real.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ocr_bifunction.reader import ProvenanceSpan, TextLine

# The verdict engine moved to `validation.py` at the extraction/verdict seam; its public names
# are re-exported here (the historical home) so the existing importers are unchanged. `X as X` =
# intentional re-export (same convention as repository.py re-exporting status.py). New code may
# import straight from `ocr_bifunction.validation`.
from ocr_bifunction.validation import (
    REJECT as REJECT,
    REVIEW as REVIEW,
    AttestationReference as AttestationReference,
    CheckFailure as CheckFailure,
    ValidationContext as ValidationContext,
    ValidationOutcome as ValidationOutcome,
    evaluate_validation as evaluate_validation,
    validate_fields as validate_fields,
)

# Horizontal tolerance (pixels) for "same column": a value counts as below a label
# when their left edges line up within this band. Tuned on ~1100px-wide CI scans.
COLUMN_X_TOLERANCE = 60.0
# Vertical tolerance (pixels) for "same row" (direction "right").
ROW_Y_TOLERANCE = 25.0

# How a field's value was obtained — the honest label on its provenance, not a quality score.
ORIGIN_ANCHOR = "anchor"  # geometry path: the value is one read line, box known exactly
ORIGIN_PATTERN = "pattern"  # regex path: spans are the line(s) the match overlapped
ORIGIN_MRZ = "mrz"  # CI backfill from the parsed machine-readable zone — NO geometry
# A reviewer typed it (D3 field_corrections, applied to D1 on accept): authoritative, and by
# nature WITHOUT geometry — a typed value sits nowhere on the page.
ORIGIN_HUMAN = "human"


@dataclass
class ExtractedField:
    """One extracted value AND where it came from in the source document.

    The product requirement is "field -> page -> show me the zone": a reviewer can only
    validate or correct a value if they can see the region it was read from. Geometry exists
    at read time (`TextLine` carries page + bbox) and used to be destroyed here, at the last
    point it was still available — so it is carried instead.

    `spans` is EMPTY whenever provenance genuinely does not exist (an MRZ-backfilled CI field
    has no box; a regex that matched nothing has no line). Absent provenance stays absent — it
    is never fabricated, and an empty list is the signal a reviewer cannot be shown a zone.
    """

    value: str | None
    spans: list[ProvenanceSpan] = field(default_factory=list)
    origin: str | None = None  # ORIGIN_* — None when no value was found at all


def field_values(extracted: dict[str, ExtractedField]) -> dict[str, str | None]:
    """Project to the plain `name -> value` mapping — the VERDICT engine's input.

    The validation checks reason about values, never about geometry: this is the seam that
    keeps `validate_fields`/`evaluate_validation` (and the drafting/naming/suggestion gates)
    unchanged while the extraction itself grew richer.
    """
    return {name: extracted_field.value for name, extracted_field in extracted.items()}


def field_payload(extracted: dict[str, ExtractedField]) -> dict[str, dict]:
    """Serialize to the JSON shape D1 stores in `ocr_jobs.record_fields`.

    One shape for BOTH lanes (structured and CI) — a column that changed shape depending on
    which lane wrote it would be unreadable for IT. `bbox` becomes a list because that is what
    JSON round-trips to; nothing downstream indexes it as a tuple.
    """
    return {
        name: {
            "value": extracted_field.value,
            "origin": extracted_field.origin,
            "spans": [
                {"page_index": span.page_index, "bbox": list(span.bbox)}
                for span in extracted_field.spans
            ],
        }
        for name, extracted_field in extracted.items()
    }


def payload_value(record_fields: dict[str, dict], name: str) -> str | None:
    """Read one value back out of the D1 payload — the counterpart of `field_payload`.

    Consumers that only need a value (corroboration, projections for the human) should not
    have to know the payload's inner shape. An absent field reads as None, like an empty one.
    """
    entry = record_fields.get(name)
    return entry.get("value") if entry else None


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
    candidates = [
        line
        for line in lines
        if line is not anchor_line
        and line.page_index == anchor_line.page_index
        and line.y0 > anchor_line.y0 + 5
        and abs(line.x0 - anchor_line.x0) <= COLUMN_X_TOLERANCE
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda line: line.y0 - anchor_line.y0)


def _value_right(lines: list[TextLine], anchor_line: TextLine) -> TextLine | None:
    candidates = [
        line
        for line in lines
        if line is not anchor_line
        and line.page_index == anchor_line.page_index
        and line.x0 >= anchor_line.x1 - 5
        and abs(line.y0 - anchor_line.y0) <= ROW_Y_TOLERANCE
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda line: line.x0 - anchor_line.x1)


def _normalize_value(value: str, rule: str) -> str:
    value = value.strip()
    if rule == "date_ddmmyyyy":
        digits = re.sub(r"\D", "", value)
        if len(digits) == 8:
            return f"{digits[4:]}-{digits[2:4]}-{digits[0:2]}"  # DDMMYYYY -> ISO
    if rule == "amount":
        # French thousands separators (space / NBSP / narrow NBSP) -> bare number.
        return re.sub(r"[\s  ]", "", value)
    if rule == "upper":
        return value.upper()
    return value


def _line_character_ranges(lines: list[TextLine]) -> list[tuple[int, int]]:
    """The `[start, end)` slice each line occupies in `"\\n".join(line.text for line in lines)`.

    The pattern path matches over the JOINED text, where line identity is gone; this map is
    the only way back to geometry. The `+ 1` is the newline the join inserts BETWEEN lines —
    it belongs to no line, so a match landing on it alone yields no span.
    """
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for line in lines:
        ranges.append((cursor, cursor + len(line.text)))
        cursor += len(line.text) + 1
    return ranges


def _spans_for_character_range(
    lines: list[TextLine], start: int, end: int
) -> list[ProvenanceSpan]:
    """Provenance of every line the `[start, end)` slice of the joined text overlaps.

    A LIST, not one box: a regex may match across the join, so a value can straddle several
    lines — and therefore several pages. Same shape as a RAG chunk's spans, for the same
    reason. An empty range (`start == end`) overlaps nothing and yields [].
    """
    spans: list[ProvenanceSpan] = []
    for line, (line_start, line_end) in zip(lines, _line_character_ranges(lines)):
        if not (line_start < end and start < line_end):
            continue
        # The slice of THIS line the match covers, in the line's own offsets.
        narrowed = _word_level_bbox(
            line,
            max(start - line_start, 0),
            min(end - line_start, len(line.text)),
        )
        # from_line returns None for a read with no page geometry: no provenance, not a fake
        # one. `narrowed` is None when the backend gave no word grain -> the whole line.
        span = ProvenanceSpan.from_line(line, bbox=narrowed)
        if span is not None:
            spans.append(span)
    return spans


def _word_level_bbox(
    line: TextLine, local_start: int, local_end: int
) -> tuple[float, float, float, float] | None:
    """Box around ONLY the words the `[local_start, local_end)` slice of the line occupies.

    A born-digital block is paragraph-sized (measured: up to 30 % of a page), so the block box
    tells a reviewer "somewhere in here". Narrowing to the covered words shrank the real
    invoice's boxes by 3.7x to 7.7x in area.

    Selection is by OFFSET OVERLAP — the words are picked by where they sit in the text, never
    by matching their spelling against the value (which lands on the wrong occurrence; measured,
    it made a box 3x bigger). None when the backend exposes no word grain (every OCR engine:
    its boxes are already line-sized) so the caller keeps the whole line.
    """
    covered = [
        word
        for word in line.word_spans
        if word.start < local_end and local_start < word.end
    ]
    if not covered:
        return None
    return (
        min(word.bbox[0] for word in covered),
        min(word.bbox[1] for word in covered),
        max(word.bbox[2] for word in covered),
        max(word.bbox[3] for word in covered),
    )


def _extract_by_pattern(
    lines: list[TextLine], document_text: str, field_definition: dict
) -> ExtractedField:
    """Extract a field by regex over the document text (group 1, else the whole match).

    Born-digital PDFs glue a label to its value inside one PyMuPDF block, so geometry
    anchors do not apply — these fields name a regex instead. Provenance is recovered from
    the match's character range: the span is that of the VALUE group, not of the whole
    match, because the zone shown to a reviewer must be the value, not its label.
    """
    match = re.search(field_definition["pattern"], document_text)
    if match is None:
        return ExtractedField(value=None)
    group_index = 1 if match.groups() else 0
    return ExtractedField(
        value=_normalize_value(
            match.group(group_index), field_definition.get("normalize", "strip")
        ),
        spans=_spans_for_character_range(lines, *match.span(group_index)),
        origin=ORIGIN_PATTERN,
    )


def extract_fields(lines: list[TextLine], template: dict) -> dict[str, ExtractedField]:
    """Rebuild the template's named fields WITH their provenance, by anchors OR by patterns.

    A field with a `pattern` key is extracted by regex over the document text (born-digital
    invoices, where PyMuPDF glues label+value in one block). A field with an `anchor` key
    uses the geometry path (scanned cards). A template may mix both — and BOTH paths carry
    page + bbox, so "show me the zone this value came from" works on either lane.

    Callers that only need values project with `field_values` (the verdict engine's input).
    """
    document_text = "\n".join(line.text for line in lines)
    extracted: dict[str, ExtractedField] = {}
    for field_definition in template["fields"]:
        name = field_definition["name"]
        if "pattern" in field_definition:
            extracted[name] = _extract_by_pattern(
                lines, document_text, field_definition
            )
            continue
        anchor_line = _find_anchor_line(lines, field_definition["anchor"])
        if anchor_line is None:
            extracted[name] = ExtractedField(value=None)
            continue
        direction = field_definition.get("direction", "below")
        if direction == "right":
            value_line = _value_right(lines, anchor_line)
        else:
            value_line = _value_below(lines, anchor_line)
        if value_line is None:
            extracted[name] = ExtractedField(value=None)
            continue
        value_span = ProvenanceSpan.from_line(value_line)
        extracted[name] = ExtractedField(
            value=_normalize_value(
                value_line.text, field_definition.get("normalize", "strip")
            ),
            # No page geometry (VLM lane) -> the value stands, its location does not exist.
            spans=[value_span] if value_span is not None else [],
            origin=ORIGIN_ANCHOR,
        )
    return extracted
