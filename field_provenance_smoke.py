"""Field-provenance smoke — extraction stops destroying geometry (template.py).

    uv run python field_provenance_smoke.py

Runs NO reader, NO OCR and needs NO document: `extract_fields` is pure over `TextLine`s, so
its whole contract is exercised on hand-built lines. PII-free (fabricated labels/values only).

The point being proved: `extract_fields` used to return `dict[str, str | None]` and threw away
`TextLine.bbox` / `.page_index` at the LAST place they still existed. It now returns
`ExtractedField(value, spans, origin)`, so a reviewer can be shown the zone a value was read
from — the "node -> page -> zone" product requirement.

Proves:
  1. anchor path (direction below/right) carries the VALUE line's page + bbox, not the label's;
  2. pattern path (born-digital invoices, regex over the JOINED text) recovers geometry from
     the match's character range — the span is the VALUE group, not the whole match;
  3. a pattern match STRADDLING two lines yields BOTH spans (hence a list, not one box);
  4. multi-page: the span carries the page the value was actually read from;
  5. absent provenance stays absent and is NEVER fabricated — a missing anchor and a
     non-matching pattern both give value=None, spans=[], origin=None;
  6. `field_values` projects back to the exact `name -> value` mapping the verdict engine
     consumes (the seam that leaves `validate_fields` untouched);
  7. `field_payload` is JSON-round-trippable — D1 stores it in `ocr_jobs.record_fields`.
"""

from __future__ import annotations

import json

from ocr_bifunction.reader import TextLine
from ocr_bifunction.template import (
    ExtractedField,
    extract_fields,
    field_payload,
    field_values,
)

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def run() -> int:
    # --- 1. Anchor path: the span is the VALUE's box, never the label's. ---------------
    label_line = TextLine("Nom", (10.0, 10.0, 60.0, 25.0), page_index=0)
    value_line = TextLine("DUPONT", (10.0, 30.0, 90.0, 45.0), page_index=0)
    right_label = TextLine("Numero", (200.0, 10.0, 260.0, 25.0), page_index=0)
    right_value = TextLine("X4T29", (270.0, 10.0, 330.0, 25.0), page_index=0)
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
        "anchor/below carries the VALUE line's bbox (not the label's)",
        anchored["nom"].value == "DUPONT"
        and anchored["nom"].origin == "anchor"
        and [(span.page_index, span.bbox) for span in anchored["nom"].spans]
        == [(0, (10.0, 30.0, 90.0, 45.0))],
    )
    _check(
        "anchor/right carries the value to the right, with its own box",
        anchored["numero"].value == "X4T29"
        and [(span.page_index, span.bbox) for span in anchored["numero"].spans]
        == [(0, (270.0, 10.0, 330.0, 25.0))],
    )

    # --- 2+3+4. Pattern path: geometry recovered from the match's character range. -----
    pattern_lines = [
        TextLine("Facture N. F-2026-0042", (0.0, 0.0, 300.0, 12.0), page_index=0),
        TextLine("Total HT 1 250,00", (0.0, 20.0, 300.0, 32.0), page_index=0),
        TextLine("Reference du marche", (0.0, 0.0, 300.0, 12.0), page_index=3),
        TextLine("MARCHE-77 suite", (0.0, 20.0, 300.0, 32.0), page_index=3),
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
        and [
            (span.page_index, span.bbox) for span in by_pattern["numero_facture"].spans
        ]
        == [(0, (0.0, 0.0, 300.0, 12.0))],
    )
    _check(
        "pattern span points at the line the value sits on, and normalization still applies",
        by_pattern["total_ht"].value == "1250,00"
        and [span.bbox for span in by_pattern["total_ht"].spans]
        == [(0.0, 20.0, 300.0, 32.0)],
    )
    _check(
        "a match straddling the join yields BOTH lines' spans (a list, not one box)",
        len(by_pattern["marche"].spans) == 2
        and [span.page_index for span in by_pattern["marche"].spans] == [3, 3]
        and [span.bbox for span in by_pattern["marche"].spans]
        == [(0.0, 0.0, 300.0, 12.0), (0.0, 20.0, 300.0, 32.0)],
    )
    _check(
        "multi-page: the span carries the page the value was actually read from",
        by_pattern["numero_facture"].spans[0].page_index == 0
        and by_pattern["marche"].spans[0].page_index == 3,
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

    # --- 6. The projection the verdict engine consumes is unchanged. ------------------
    _check(
        "field_values projects to the exact name -> value mapping (verdict engine seam)",
        field_values(anchored) == {"nom": "DUPONT", "numero": "X4T29"}
        and field_values(by_pattern)["absent"] is None,
    )

    # --- 7. The D1 payload survives a JSON round-trip. --------------------------------
    payload = field_payload(anchored)
    round_tripped = json.loads(json.dumps(payload, ensure_ascii=False))
    _check(
        "field_payload round-trips through JSON with value + origin + spans intact",
        round_tripped["nom"]
        == {
            "value": "DUPONT",
            "origin": "anchor",
            "spans": [{"page_index": 0, "bbox": [10.0, 30.0, 90.0, 45.0]}],
        },
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT field provenance (page + bbox survive extraction): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
