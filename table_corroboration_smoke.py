"""Table-corroboration smoke — the narrow second opinion (table_corroboration.py).

    uv run python table_corroboration_smoke.py

Runs NO reader: the comparison is pure, so its whole contract is exercised on hand-written markdown
pairs. PII-free, no Docling, no markitdown call, no document.

Proves:
  1. identical tables -> corroborated, and `has_tables` marks the agreement as non-vacuous;
  2. a GARBLED table (columns collapsed by merged cells) -> divergence, shape named in the reason;
  3. a table MISSED entirely by one reader -> divergence on the count;
  4. two readers that both found nothing -> `has_tables` False, so the caller can tell a real
     agreement from a vacuous one;
  5. the alignment row is markup, not data: it never inflates the row count;
  6. surrounding prose and multiple tables do not confuse the extraction (reading order preserved).
"""

from __future__ import annotations

from ocr_bifunction.table_corroboration import (
    TableProfile,
    corroborate_tables,
    extract_table_profiles,
)

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


CLEAN_TABLE = """| Poste | Quantite | Prix |
| --- | --- | --- |
| Cable | 12 | 340 |
| Coffret | 3 | 210 |
"""

# The same table as a neural reconstruction might garble it: two columns fused into one.
GARBLED_TABLE = """| Poste | Quantite Prix |
| --- | --- |
| Cable | 12 340 |
| Coffret | 3 210 |
"""


def run() -> int:
    # 1. Two readers that reconstructed the same table agree.
    same = corroborate_tables(CLEAN_TABLE, CLEAN_TABLE)
    _check(
        "identical tables -> corroborated, agreement is non-vacuous",
        same.corroborated and same.has_tables and same.table_count_a == 1,
    )

    # 2. A garbled table changes SHAPE — that is the whole detection premise.
    garbled = corroborate_tables(CLEAN_TABLE, GARBLED_TABLE)
    _check(
        "garbled table (columns collapsed) -> divergence, shape named",
        not garbled.corroborated
        and any("shape differs" in reason for reason in garbled.reasons),
    )

    # 3. A table one reader missed entirely shows up as a count disagreement.
    missed = corroborate_tables(CLEAN_TABLE, "Just prose, no table at all.\n")
    _check(
        "table missed by one reader -> divergence on the count",
        not missed.corroborated
        and any("table count differs" in reason for reason in missed.reasons),
    )

    # 4. Both readers finding nothing is agreement about NOTHING — the caller must be able to tell.
    empty = corroborate_tables("Just prose.\n", "Only prose here too.\n")
    _check(
        "no tables anywhere -> corroborated but has_tables False (vacuous, not evidence)",
        empty.corroborated and not empty.has_tables,
    )

    # 5. The alignment row is markup: it must not count as a data row.
    profiles = extract_table_profiles(CLEAN_TABLE)
    _check(
        "alignment row excluded: 3 data rows (header + 2), 3 columns",
        profiles == [TableProfile(row_count=3, column_count=3)],
    )

    # 6. Prose around and between tables must not merge or hide them.
    document = f"Intro paragraphe.\n\n{CLEAN_TABLE}\nTexte entre les deux.\n\n{GARBLED_TABLE}\nFin."
    multiple = extract_table_profiles(document)
    _check(
        "two tables separated by prose -> both found, in reading order",
        len(multiple) == 2
        and multiple[0].column_count == 3
        and multiple[1].column_count == 2,
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT table corroboration (the narrow second opinion): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
