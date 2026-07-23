"""Text-integrity guard — the CHARACTER-INTEGRITY sub-edge of conversion validation.

Companion to conversion_guard.py (completeness) and the layout_score signal (form). Those
answer "did every page come out?" and "is the structure recovered?". This one answers a
question NEITHER can: are the extracted CHARACTERS themselves the right ones?

Why it is needed, and why it is model-agnostic: on a born-digital PDF the text is not
recognised, it is read from the PDF's embedded ToUnicode CMap. A subset font with an absent or
broken CMap yields MOJIBAKE ("Ã©", "â€™") on a document that is otherwise perfectly native — and
EVERY text-layer extractor (Docling, markitdown, PyMuPDF) trusts that SAME CMap, so they all
produce the SAME wrong text. The corruption is a property of the SOURCE, not the reader:
swapping the model does not help, and a second reader cannot corroborate it away (both agree on
the garbage). The only defence is an INTRINSIC check on the extracted text, whoever produced
it — hence this guard sits ABOVE the reader slot and takes a plain str, never a Docling object
and never a lane.

Two distinct failure classes, kept separate (measured against ftfy 6.3.1, 2026-07-20):

  1. IRREVERSIBLE LOSS — a U+FFFD replacement character is already in the text. The original
     byte is GONE; no tool recovers it. Verified: `ftfy.badness.is_bad()` returns False on a
     U+FFFD string — it flags only *reversible* mojibake — so this class MUST be checked
     independently. It is NOT redundant with the mojibake heuristic.

  2. REPAIRABLE MOJIBAKE — UTF-8 bytes decoded as latin-1 / cp1252. Detected by
     `ftfy.badness.is_bad()`; `ftfy.fix_and_explain()` reverses it AND explains the byte steps.
     Verified reversible: "été" -> "Ã©tÃ©" round-trips back to the exact original.

Repair is offered as a SUGGESTION, never applied silently: a repairable text yields a
`repaired_text` candidate for a human to validate (same "propose / human disposes" doctrine as
the drafting lane). Irreversible loss is a hard flag with NO repair candidate.

PURE: no I/O, no store, no model. The caller feeds it the extracted string and routes the
disposition onto the auto / review gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# The Unicode replacement character: its presence means a decode already gave up on some bytes,
# so the information is destroyed and no repair can bring it back.
REPLACEMENT_CHARACTER = "�"

TextIntegrityDisposition = Literal[
    "clean", "repairable_mojibake", "irreversible_loss", "suspect_encoding"
]


@dataclass
class TextIntegrityAssessment:
    """The verdict on ONE extracted text: are its characters trustworthy?

    `disposition` is the load-bearing field:
      - "clean"               — no corruption signal; pass.
      - "repairable_mojibake" — reversible encoding mix-up; `repaired_text` holds the candidate
                                a human validates (NEVER auto-applied).
      - "irreversible_loss"   — U+FFFD present; data already destroyed, NO repair possible.
      - "suspect_encoding"    — flagged corrupt by the heuristic but not cleanly reversible.

    `badness_score` is ftfy's numeric heuristic (0 = clean, higher = worse) — the calibratable
    observability number, mirroring how conversion_guard exposes layout scores. `repair_operations`
    carries the byte steps ftfy would apply (e.g. [("encode", "latin-1"), ("decode", "utf-8")]),
    so a reviewer sees WHY the candidate is proposed, not just the candidate."""

    character_count: int
    replacement_character_count: int
    is_mojibake: bool
    badness_score: int
    repaired_text: str | None
    repair_operations: list[tuple[str, str]]
    disposition: TextIntegrityDisposition
    reasons: list[str] = field(default_factory=list)


def assess_text_integrity(text: str) -> TextIntegrityAssessment:
    """Intrinsic character-integrity check on one extracted string (model-agnostic, PURE).

    Precedence mirrors the reject > review > auto spirit: an irreversible loss (U+FFFD) is the
    hardest signal and wins over a repairable mojibake, which in turn wins over a bare heuristic
    flag. A caller maps the returned disposition onto the auto / review gate."""
    import ftfy
    import ftfy.badness

    character_count = len(text)
    replacement_character_count = text.count(REPLACEMENT_CHARACTER)
    has_irreversible_loss = replacement_character_count > 0
    is_mojibake = ftfy.badness.is_bad(text)
    badness_score = ftfy.badness.badness(text)

    repaired_text: str | None = None
    repair_operations: list[tuple[str, str]] = []
    if is_mojibake and not has_irreversible_loss:
        explained = ftfy.fix_and_explain(text)
        # A candidate we would SUGGEST must actually change the text AND introduce no new loss.
        if explained.text != text and REPLACEMENT_CHARACTER not in explained.text:
            repaired_text = explained.text
            repair_operations = [tuple(step) for step in explained.explanation]

    reasons: list[str] = []
    if has_irreversible_loss:
        disposition: TextIntegrityDisposition = "irreversible_loss"
        reasons.append(
            f"irreversible loss: {replacement_character_count} U+FFFD replacement "
            "character(s) — original bytes destroyed, no repair possible"
        )
    elif repaired_text is not None:
        disposition = "repairable_mojibake"
        reasons.append(
            f"repairable mojibake (badness {badness_score}): encoding mix-up detected, "
            "a repair candidate is offered for human validation"
        )
    elif is_mojibake:
        disposition = "suspect_encoding"
        reasons.append(
            f"suspect encoding (badness {badness_score}): flagged corrupt but not cleanly "
            "reversible — route to human"
        )
    else:
        disposition = "clean"

    return TextIntegrityAssessment(
        character_count=character_count,
        replacement_character_count=replacement_character_count,
        is_mojibake=is_mojibake,
        badness_score=badness_score,
        repaired_text=repaired_text,
        repair_operations=repair_operations,
        disposition=disposition,
        reasons=reasons,
    )
