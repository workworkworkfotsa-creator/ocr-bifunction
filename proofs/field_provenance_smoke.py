"""Field-provenance smoke — extraction stops destroying geometry (template.py).

    uv run python proofs/field_provenance_smoke.py

Runs NO reader, NO OCR and needs NO document: `extract_fields` is pure over `TextLine`s, so
its whole contract is exercised on hand-built lines. PII-free (fabricated labels/values only).

The point being proved: `extract_fields` used to return `dict[str, str | None]` and threw away
`TextLine.bbox` / `.page_index` at the LAST place they still existed. It now returns
`ExtractedField(value, spans, origin)`, so a reviewer can be shown the zone a value was read
from — the "node -> page -> zone" product requirement.

Spans are NORMALIZED to the page (fractions in [0, 1]): the reader's native units differ by
backend — PDF points at 72 dpi for a text layer, pixels of a 200 dpi render for OCR — so a raw
box is unplaceable without knowing which. Normalized, a consumer draws it with no unit, no dpi
and no page size to carry. The lines below therefore declare page_width/page_height, and the
expectations are fractions.

Proves:
  1. anchor path (direction below/right) carries the VALUE line's page + box, not the label's;
  2. pattern path (born-digital invoices, regex over the JOINED text) recovers geometry from
     the match's character range — the span is the VALUE group, not the whole match;
  3. a pattern match STRADDLING two lines yields BOTH spans (hence a list, not one box);
  4. multi-page: the span carries the page the value was actually read from;
  4bis. per-word narrowing shrinks the span from the paragraph-sized block to the value's own
     words, selected BY OFFSET — two values on the same line give two different boxes, which a
     spelling lookup cannot do; a backend with no word grain falls back to the whole line;
  5. absent provenance stays absent and is NEVER fabricated — a missing anchor and a
     non-matching pattern both give value=None, spans=[], origin=None;
  6. NO page geometry (the VLM lane, whose boxes are synthetic reading-order scaffolding)
     yields a VALUE with NO span — never a location normalized against a guessed frame;
  7. `field_values` projects back to the exact `name -> value` mapping the verdict engine
     consumes (the seam that leaves `validate_fields` untouched);
  8. `field_payload` is JSON-round-trippable — D1 stores it in `ocr_jobs.record_fields`.
"""

from __future__ import annotations

import json

from ocr_bifunction.reading.reader import TextLine, WordSpan
from ocr_bifunction.extraction.template import (
    ExtractedField,
    extract_fields,
    field_payload,
    field_values,
)

CHECKS: list[tuple[str, bool]] = []

# The two fabricated page frames the lines below are positioned in.
ANCHOR_PAGE = {"page_width": 1000.0, "page_height": 500.0}
PATTERN_PAGE = {"page_width": 600.0, "page_height": 800.0}


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _boxes_close(
    spans: list, expected: list[tuple[float, float, float, float]]
) -> bool:
    """Compare normalized boxes with a float tolerance (they come out of a division)."""
    if len(spans) != len(expected):
        return False
    return all(
        all(abs(actual - wanted) < 1e-9 for actual, wanted in zip(span.bbox, box))
        for span, box in zip(spans, expected)
    )


def run() -> int:
    # --- 1. Anchor path: the span is the VALUE's box, never the label's. ---------------
    label_line = TextLine("Nom", (10.0, 10.0, 60.0, 25.0), page_index=0, **ANCHOR_PAGE)
    value_line = TextLine(
        "DUPONT", (10.0, 30.0, 90.0, 45.0), page_index=0, **ANCHOR_PAGE
    )
    right_label = TextLine(
        "Numero", (200.0, 10.0, 260.0, 25.0), page_index=0, **ANCHOR_PAGE
    )
    right_value = TextLine(
        "X4T29", (270.0, 10.0, 330.0, 25.0), page_index=0, **ANCHOR_PAGE
    )
    anchor_lines = [label_line, value_line, right_label, right_value]
    anchor_template = {
        "template_id": "smoke_anchor",
        "fields": [
            {"name": "nom", "anchor": "Nom"},
            {"name": "numero", "anchor": "Numero", "direction": "right"},
        ],
    }
    anchored = extract_fields(anchor_lines, anchor_template)
    _check(
        "anchor/below carries the VALUE line's box, normalized (not the label's)",
        anchored["nom"].value == "DUPONT"
        and anchored["nom"].origin == "anchor"
        and [span.page_index for span in anchored["nom"].spans] == [0]
        # (10, 30, 90, 45) over a 1000x500 page
        and _boxes_close(anchored["nom"].spans, [(0.01, 0.06, 0.09, 0.09)]),
    )
    _check(
        "anchor/right carries the value to the right, with its own normalized box",
        anchored["numero"].value == "X4T29"
        # (270, 10, 330, 25) over a 1000x500 page
        and _boxes_close(anchored["numero"].spans, [(0.27, 0.02, 0.33, 0.05)]),
    )
    _check(
        "every normalized coordinate lands inside [0, 1]",
        all(
            0.0 <= coordinate <= 1.0
            for extracted in anchored.values()
            for span in extracted.spans
            for coordinate in span.bbox
        ),
    )

    # --- 2+3+4. Pattern path: geometry recovered from the match's character range. -----
    pattern_lines = [
        TextLine("Facture N. F-2026-0042", (0.0, 0.0, 300.0, 12.0), **PATTERN_PAGE),
        TextLine("Total HT 1 250,00", (0.0, 20.0, 300.0, 32.0), **PATTERN_PAGE),
        TextLine(
            "Reference du marche", (0.0, 0.0, 300.0, 12.0), page_index=3, **PATTERN_PAGE
        ),
        TextLine(
            "MARCHE-77 suite", (0.0, 20.0, 300.0, 32.0), page_index=3, **PATTERN_PAGE
        ),
    ]
    pattern_template = {
        "template_id": "smoke_pattern",
        "fields": [
            {"name": "numero_facture", "pattern": r"Facture N\. (\S+)"},
            {
                "name": "total_ht",
                "pattern": r"Total HT ([\d  ,]+)",
                "normalize": "amount",
            },
            # Matches ACROSS the newline the join inserts -> must yield two spans.
            {"name": "marche", "pattern": r"(marche\nMARCHE-\d+)"},
            {"name": "absent", "pattern": r"Mention introuvable (\S+)"},
        ],
    }
    by_pattern = extract_fields(pattern_lines, pattern_template)
    _check(
        "pattern path recovers the span of the VALUE group (not the whole match)",
        by_pattern["numero_facture"].value == "F-2026-0042"
        and by_pattern["numero_facture"].origin == "pattern"
        and [span.page_index for span in by_pattern["numero_facture"].spans] == [0]
        # (0, 0, 300, 12) over a 600x800 page
        and _boxes_close(by_pattern["numero_facture"].spans, [(0.0, 0.0, 0.5, 0.015)]),
    )
    _check(
        "pattern span points at the line the value sits on, and normalization still applies",
        by_pattern["total_ht"].value == "1250,00"
        and _boxes_close(by_pattern["total_ht"].spans, [(0.0, 0.025, 0.5, 0.04)]),
    )
    _check(
        "a match straddling the join yields BOTH lines' spans (a list, not one box)",
        len(by_pattern["marche"].spans) == 2
        and [span.page_index for span in by_pattern["marche"].spans] == [3, 3]
        and _boxes_close(
            by_pattern["marche"].spans,
            [(0.0, 0.0, 0.5, 0.015), (0.0, 0.025, 0.5, 0.04)],
        ),
    )
    _check(
        "multi-page: the span carries the page the value was actually read from",
        by_pattern["numero_facture"].spans[0].page_index == 0
        and by_pattern["marche"].spans[0].page_index == 3,
    )

    # --- 4bis. Word-level narrowing: BY POSITION, never by spelling. -------------------
    # One born-digital block holds a whole paragraph, so the block box says "somewhere in
    # here". With per-word offsets the span shrinks to the words the value actually occupies.
    # The decisive case: TWO values on the SAME line must give TWO different boxes — which a
    # spelling lookup cannot do (measured on a real invoice, matching by text picked up the
    # same words from the document title and made the box 3x BIGGER).
    worded_line = TextLine(
        "Facture N. F-2026-0042 du 03/07/2026",
        (0.0, 0.0, 300.0, 12.0),
        **PATTERN_PAGE,
        word_spans=[
            WordSpan(0, 7, (0.0, 0.0, 50.0, 12.0)),  # Facture
            WordSpan(8, 10, (55.0, 0.0, 65.0, 12.0)),  # N.
            WordSpan(11, 22, (70.0, 0.0, 140.0, 12.0)),  # F-2026-0042
            WordSpan(23, 25, (145.0, 0.0, 160.0, 12.0)),  # du
            WordSpan(26, 36, (165.0, 0.0, 230.0, 12.0)),  # 03/07/2026
        ],
    )
    worded = extract_fields(
        [worded_line],
        {
            "template_id": "smoke_words",
            "fields": [
                {"name": "numero", "pattern": r"Facture N\. (\S+)"},
                {"name": "date", "pattern": r"du (\S+)"},
            ],
        },
    )
    _check(
        "word grain narrows the span to the value's own words, not the whole block",
        # word box (70, 0, 140, 12) over a 600x800 page — NOT the line's (0, 0, 300, 12)
        _boxes_close(worded["numero"].spans, [(70 / 600, 0.0, 140 / 600, 12 / 800)]),
    )
    _check(
        "two values on the SAME line give two DIFFERENT boxes (position, not spelling)",
        _boxes_close(worded["date"].spans, [(165 / 600, 0.0, 230 / 600, 12 / 800)])
        and worded["numero"].spans[0].bbox != worded["date"].spans[0].bbox,
    )
    _check(
        "narrowed box is strictly inside the line's own box",
        worded["numero"].spans[0].bbox[0] > 0.0
        and worded["numero"].spans[0].bbox[2] < 300.0 / 600,
    )
    _check(
        "no word grain (every OCR engine) -> the WHOLE line box, unchanged fallback",
        _boxes_close(by_pattern["numero_facture"].spans, [(0.0, 0.0, 0.5, 0.015)]),
    )

    # --- 5. Absent provenance stays absent — never fabricated. -------------------------
    missing_anchor = extract_fields(
        anchor_lines,
        {"template_id": "smoke_missing", "fields": [{"name": "vide", "anchor": "Zzz"}]},
    )
    _check(
        "no anchor found -> value None, spans [], origin None (nothing invented)",
        missing_anchor["vide"] == ExtractedField(value=None, spans=[], origin=None),
    )
    _check(
        "pattern that matches nothing -> value None, spans [], origin None",
        by_pattern["absent"] == ExtractedField(value=None, spans=[], origin=None),
    )
    label_only = extract_fields(
        [label_line],
        {"template_id": "smoke_label", "fields": [{"name": "nom", "anchor": "Nom"}]},
    )
    _check(
        "anchor found but NO value line -> still no fabricated span",
        label_only["nom"].value is None and label_only["nom"].spans == [],
    )

    # --- 6. No page frame (the VLM lane) -> a value, but NO location. ------------------
    # LightOnOCR emits synthetic top-to-bottom boxes that encode reading ORDER, not position,
    # so it declares no page size. Normalizing against a guessed frame would fabricate a zone.
    frameless_lines = [
        TextLine("Nom", (0.0, 0.0, 1000.0, 10.0)),
        TextLine("DUPONT", (0.0, 12.0, 1000.0, 22.0)),
    ]
    frameless = extract_fields(frameless_lines, anchor_template)
    _check(
        "no page geometry -> the VALUE is still extracted (the lane keeps working)",
        frameless["nom"].value == "DUPONT" and frameless["nom"].origin == "anchor",
    )
    _check(
        "no page geometry -> NO span, rather than one against a guessed frame",
        frameless["nom"].spans == [],
    )

    # --- 7. The projection the verdict engine consumes is unchanged. ------------------
    _check(
        "field_values projects to the exact name -> value mapping (verdict engine seam)",
        field_values(anchored) == {"nom": "DUPONT", "numero": "X4T29"}
        and field_values(by_pattern)["absent"] is None,
    )

    # --- 8. The D1 payload survives a JSON round-trip. --------------------------------
    payload = field_payload(anchored)
    round_tripped = json.loads(json.dumps(payload, ensure_ascii=False))
    _check(
        "field_payload round-trips through JSON with value + origin + spans intact",
        round_tripped["nom"]
        == {
            "value": "DUPONT",
            "origin": "anchor",
            "spans": [{"page_index": 0, "bbox": [0.01, 0.06, 0.09, 0.09]}],
        },
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT field provenance (normalized page + box survive extraction): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
