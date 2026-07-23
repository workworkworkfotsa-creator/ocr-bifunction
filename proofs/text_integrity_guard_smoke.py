"""Text-integrity guard smoke — the character-integrity sub-edge (text_integrity_guard.py).

    uv run python proofs/text_integrity_guard_smoke.py

Runs NO reader and needs NO document: the guard is a PURE check on an extracted string, so its
whole contract is exercised on mojibake built PROGRAMMATICALLY. Lesson baked in: never build a
mojibake sample from a shell/source literal — a terminal's own encoding corrupts it before the
test even runs; `original.encode("utf-8").decode("latin-1")` is the reliable reproducer.
PII-free: the only content is the fabricated sample "été à Noël".

Proves (measured against ftfy 6.3.1, 2026-07-20):
  1. clean French text -> "clean", not flagged, no repair candidate;
  2. reversible mojibake (UTF-8 bytes read as latin-1) -> "repairable_mojibake", the repair
     candidate round-trips back to the EXACT original, and the byte steps are explained;
  3. irreversible loss (a U+FFFD already in the text) -> "irreversible_loss", NO repair
     candidate — AND the mojibake heuristic alone would MISS it (`is_bad` is False on U+FFFD),
     which is exactly why the replacement-character check is a separate, non-redundant signal;
  4. false-positive safety: clean accented prose is NOT flagged.
"""

from __future__ import annotations

import ftfy.badness

from ocr_bifunction.reading.text_integrity_guard import assess_text_integrity

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def run() -> int:
    original = "été à Noël"

    # 1. Clean text passes untouched.
    clean = assess_text_integrity(original)
    _check(
        "clean text -> clean, not mojibake, no repair candidate",
        clean.disposition == "clean"
        and not clean.is_mojibake
        and clean.repaired_text is None
        and clean.replacement_character_count == 0,
    )

    # 2. Reversible mojibake, built PROGRAMMATICALLY (UTF-8 bytes decoded as latin-1).
    mojibake = original.encode("utf-8").decode("latin-1")
    repairable = assess_text_integrity(mojibake)
    _check(
        "reversible mojibake -> repairable, round-trips to the exact original, steps explained",
        repairable.disposition == "repairable_mojibake"
        and repairable.is_mojibake
        and repairable.repaired_text == original
        and repairable.replacement_character_count == 0
        and bool(repairable.repair_operations),
    )

    # 3. Irreversible loss: a U+FFFD is already present -> hard flag, no repair.
    lossy = "abc�def"
    lost = assess_text_integrity(lossy)
    _check(
        "U+FFFD loss -> irreversible_loss, counted, no repair candidate",
        lost.disposition == "irreversible_loss"
        and lost.replacement_character_count == 1
        and lost.repaired_text is None,
    )
    # The load-bearing reason this signal is SEPARATE: ftfy's mojibake heuristic misses U+FFFD.
    _check(
        "the mojibake heuristic alone MISSES U+FFFD loss (why the check is non-redundant)",
        ftfy.badness.is_bad(lossy) is False,
    )

    # 4. False-positive safety on richer accented prose (the real content type).
    prose = "L'humanité à Noël règle la réflexion."
    prose_assessment = assess_text_integrity(prose)
    _check(
        "clean accented prose is not flagged (no false positive)",
        prose_assessment.disposition == "clean" and not prose_assessment.is_mojibake,
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT text-integrity guard (character-integrity sub-edge): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
