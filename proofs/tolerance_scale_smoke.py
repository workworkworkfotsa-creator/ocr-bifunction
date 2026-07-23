"""Tolerance-scale smoke — the geometry rules stop depending on the capture resolution.

    uv run python proofs/tolerance_scale_smoke.py

THE BUG. `_value_below` / `_value_right` compared coordinates against ABSOLUTE PIXEL constants
(`60` for "same column", `25` for "same row", "tuned on ~1100px-wide CI scans"). A card scanned
at twice that resolution doubles every offset while the constant stays put, so the rule silently
TIGHTENS until it stops matching — a better scan extracting fewer fields. Nothing in the code
said so, and no test could see it, because the corpus only ever held one resolution.

THE FIX. The tolerances are expressed in LINE HEIGHTS, the document's own text scale. "Same
column" and "same row" are typographic properties, not properties of the sensor.

Hand-built `TextLine`s: no OCR, no document, deterministic and instant. PII-free.

Proves:
  1. the SAME layout at 1x and at 2x extracts the SAME values — resolution independence, the
     whole point;
  2. the OLD absolute rule would have FAILED on that 2x layout: the bug was real, not a
     theoretical worry (this check fails if someone reverts to a constant);
  3. it holds for the "right" direction too, whose tolerance is the row one;
  4. a value genuinely too far off-column is still REJECTED at both scales — the fix widens
     nothing, it just stops the scale from mattering;
  5. lines with no usable height fall back instead of collapsing to a zero tolerance.
"""

from __future__ import annotations

from ocr_bifunction.reading.reader import TextLine
from ocr_bifunction.extraction.template import (
    COLUMN_X_TOLERANCE_LINE_HEIGHTS,
    FALLBACK_COLUMN_X_TOLERANCE,
    extract_fields,
    field_values,
)

CHECKS: list[tuple[str, bool]] = []

# The reference capture: a line is 34 px tall, as measured on the real CI corpus.
LINE_HEIGHT = 34.0
# The value sits 40 px right of its label's left edge — inside BOTH the old constant (60) and
# the new tolerance (1.75 x 34 = 59.5) at 1x. At 2x it becomes 80: still inside the new
# tolerance (119), outside the old one (60). That is exactly the silent failure.
COLUMN_OFFSET = 40.0
OLD_ABSOLUTE_COLUMN_TOLERANCE = 60.0

TEMPLATE = {
    "template_id": "smoke_scale",
    "fields": [
        {"name": "nom", "anchor": "Nom"},
        {"name": "numero", "anchor": "Numero", "direction": "right"},
    ],
}


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _layout(scale: float) -> list[TextLine]:
    """The same page captured `scale` times larger — every coordinate multiplied, nothing else."""
    height = LINE_HEIGHT * scale
    page = {"page_width": 1066.0 * scale, "page_height": 694.0 * scale}

    def box(x0: float, y0: float, width: float) -> tuple[float, float, float, float]:
        return (x0 * scale, y0 * scale, (x0 + width) * scale, y0 * scale + height)

    return [
        TextLine("Nom", box(100, 100, 120), **page),
        TextLine("DUPONT", box(100 + COLUMN_OFFSET, 160, 200), **page),
        TextLine("Numero", box(600, 100, 130), **page),
        # Same row as its label, nudged down by half a line — inside the row tolerance.
        TextLine("X4T29", box(750, 100 + LINE_HEIGHT * 0.4, 140), **page),
    ]


def run() -> int:
    at_one = field_values(extract_fields(_layout(1.0), TEMPLATE))
    at_two = field_values(extract_fields(_layout(2.0), TEMPLATE))

    _check(
        "1x extracts both fields (the reference capture still works)",
        at_one == {"nom": "DUPONT", "numero": "X4T29"},
    )
    _check(
        "2x extracts the SAME values — the rule no longer depends on the resolution",
        at_two == at_one,
    )
    _check(
        "the 'right' direction survives the rescale too (row tolerance)",
        at_two["numero"] == "X4T29",
    )

    # --- 2. The bug was REAL: the old constant would have missed the 2x column. --------
    scaled_offset = COLUMN_OFFSET * 2.0
    new_tolerance = LINE_HEIGHT * 2.0 * COLUMN_X_TOLERANCE_LINE_HEIGHTS
    _check(
        f"the OLD absolute rule would have FAILED at 2x (offset {scaled_offset:.0f} > "
        f"{OLD_ABSOLUTE_COLUMN_TOLERANCE:.0f}), the new one holds (tolerance {new_tolerance:.0f})",
        scaled_offset > OLD_ABSOLUTE_COLUMN_TOLERANCE
        and scaled_offset <= new_tolerance,
    )

    # --- 4. The fix does not simply widen everything. ---------------------------------
    def _far_layout(scale: float) -> list[TextLine]:
        lines = _layout(scale)
        far = lines[1]
        # Push the value 4 line-heights off-column: outside the tolerance at ANY scale.
        offset = LINE_HEIGHT * 4.0 * scale
        lines[1] = TextLine(
            far.text,
            (far.bbox[0] + offset, far.bbox[1], far.bbox[2] + offset, far.bbox[3]),
            page_width=far.page_width,
            page_height=far.page_height,
        )
        return lines

    _check(
        "a value genuinely off-column is still REJECTED — at 1x and at 2x alike",
        field_values(extract_fields(_far_layout(1.0), TEMPLATE))["nom"] is None
        and field_values(extract_fields(_far_layout(2.0), TEMPLATE))["nom"] is None,
    )

    # --- 5. Degenerate read: fall back, never a zero tolerance. ------------------------
    flat = [
        TextLine("Nom", (100.0, 100.0, 220.0, 100.0)),
        TextLine("DUPONT", (100.0 + COLUMN_OFFSET, 160.0, 300.0, 160.0)),
    ]
    _check(
        f"zero-height lines fall back to the historical constant ({FALLBACK_COLUMN_X_TOLERANCE:.0f}) "
        "instead of collapsing to a zero tolerance",
        field_values(extract_fields(flat, TEMPLATE))["nom"] == "DUPONT",
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT resolution-independent geometry rules: "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
