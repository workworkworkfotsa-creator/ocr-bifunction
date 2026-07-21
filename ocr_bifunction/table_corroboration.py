"""Table corroboration — a cheap SECOND OPINION on the one thing a second reader can really check.

Scope is deliberately NARROW (decision 2026-07-21): of everything a second reader could in theory
corroborate, only TABLES pay for themselves here. The rest was measured and dropped —
character integrity cannot be corroborated at all (every text-layer reader trusts the same broken
ToUnicode CMap and agrees on the same garbage, hence `text_integrity_guard` checks it intrinsically),
and hierarchy has no second opinion to give (a pdfminer-based reader emits no headings whatsoever:
0 across 24 real documents). Tables are the exception, for two reasons:

  1. THE INDEPENDENCE IS REAL. Docling reconstructs tables with a NEURAL model (TableFormer);
     pdfplumber reconstructs them GEOMETRICALLY (ruling lines and word positions). Two unrelated
     methods, so agreement is genuine evidence rather than two copies of the same mistake.
  2. IT LANDS ON THE PROVEN WEAK SPOT. The measured Docling failure was exactly wide/dense tables
     coming out garbled (a page scoring 0.70 on layout). That is where a second opinion is worth
     paying for, and the second reader is fast (text layer only, no model, no OCR).

WHAT DIVERGENCE MEANS: a garbled table loses or merges cell boundaries, so its SHAPE changes — the
column count collapses, rows fuse. Comparing shapes therefore catches the failure without needing to
compare content, which would drown in whitespace and formatting noise. Shape is the signal; this
module deliberately does not attempt cell-by-cell diffing.

PURE: takes two markdown strings and compares them. Which readers produced them, and whether the
comparison is done per page or per document, is the caller's business — page granularity is NOT
assumed here, because a markdown converter's page separators proved unreliable on real documents
(one document reported a single page for ~100k characters).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A markdown alignment row (`|---|:--:|`) separates the header from the body and carries no data.
_ALIGNMENT_ROW = re.compile(r"^\s*\|[\s:\-|]+\|\s*$")


@dataclass(frozen=True)
class TableProfile:
    """The SHAPE of one table — what survives formatting noise and breaks when a table is garbled."""

    row_count: int
    column_count: int


@dataclass
class TableCorroboration:
    """Whether two independent readers reconstructed the same tables.

    `corroborated` is False as soon as the readers disagree on how many tables there are or on the
    shape of any aligned pair — the caller routes that to a human. `has_tables` distinguishes a
    genuine agreement from a vacuous one: two readers that both found nothing agree about nothing."""

    table_count_a: int
    table_count_b: int
    profiles_a: list[TableProfile]
    profiles_b: list[TableProfile]
    corroborated: bool
    has_tables: bool
    reasons: list[str] = field(default_factory=list)


def _row_columns(row: str) -> int:
    """Cells in one markdown row. The leading/trailing pipes produce empty edge fragments, which are
    dropped so `| a | b |` counts as 2 columns, not 4."""
    return len([cell for cell in row.strip().strip("|").split("|")])


def extract_table_profiles(markdown: str) -> list[TableProfile]:
    """Every markdown table in reading order, reduced to its shape.

    A table is a run of consecutive lines starting with `|`; the alignment row is excluded from the
    row count (it is markup, not data). The column count is the WIDEST row: a garbled table tends to
    lose cells on some rows, and taking the maximum keeps that loss visible as a shape difference
    against the other reader rather than averaging it away."""
    profiles: list[TableProfile] = []
    current_rows: list[str] = []

    def flush() -> None:
        if not current_rows:
            return
        data_rows = [row for row in current_rows if not _ALIGNMENT_ROW.match(row)]
        if data_rows:
            profiles.append(
                TableProfile(
                    row_count=len(data_rows),
                    column_count=max(_row_columns(row) for row in data_rows),
                )
            )

    for line in markdown.split("\n"):
        if line.lstrip().startswith("|"):
            current_rows.append(line)
            continue
        flush()
        current_rows = []
    flush()
    return profiles


def compare_table_profiles(
    profiles_a: list[TableProfile], profiles_b: list[TableProfile]
) -> TableCorroboration:
    """Compare two readers' table reconstructions, aligned by reading order.

    Disagreement on the COUNT is reported first and on its own: once the readers found a different
    number of tables, pairing them positionally is meaningless, so shapes are only compared over the
    overlapping prefix and the count mismatch carries the verdict."""
    reasons: list[str] = []
    count_agrees = len(profiles_a) == len(profiles_b)
    if not count_agrees:
        reasons.append(
            f"table count differs: reader A found {len(profiles_a)}, reader B found "
            f"{len(profiles_b)} — the readers did not reconstruct the same tables"
        )

    for index, (profile_a, profile_b) in enumerate(zip(profiles_a, profiles_b)):
        if profile_a != profile_b:
            reasons.append(
                f"table {index + 1} shape differs: A is {profile_a.row_count}x"
                f"{profile_a.column_count}, B is {profile_b.row_count}x"
                f"{profile_b.column_count} (rows x columns) — a garbled table loses or merges cells"
            )

    return TableCorroboration(
        table_count_a=len(profiles_a),
        table_count_b=len(profiles_b),
        profiles_a=profiles_a,
        profiles_b=profiles_b,
        corroborated=not reasons,
        has_tables=bool(profiles_a or profiles_b),
        reasons=reasons,
    )


def corroborate_tables(markdown_a: str, markdown_b: str) -> TableCorroboration:
    """One call: extract both readers' table shapes and compare them."""
    return compare_table_profiles(
        extract_table_profiles(markdown_a), extract_table_profiles(markdown_b)
    )
