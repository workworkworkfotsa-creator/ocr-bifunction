"""How two reads of the same identity are compared — one primitive, one definition.

It lived in `reconcile.py` and was imported from `validation/checks.py`, which put a cycle
between the two concerns (extraction -> validation -> extraction) for a pure string
function. It belongs to neither: reconcile compares a recto against its MRZ, the checks
compare a name against a reference or a registry, and both need the SAME key or the two
answers disagree on what "same person" means.

Transverse on purpose, so it sits at the package root rather than in a concern folder.

⚠️ STRICT by design. Folding accents makes an accented recto comparable to the ICAO
transliteration, which carries none; it does NOT loosen the match. A genuine one-character
difference still mismatches — real fraud is the close-name sibling (Ahmed / Hamed), so
tolerance here is a security trade-off, never a free convenience.
"""

from __future__ import annotations

import re
import unicodedata


def _fold_accents(text: str) -> str:
    """Strip diacritics: 'GAËLLE' -> 'GAELLE'. NFD splits an accented letter into its base
    plus a combining mark; dropping the marks leaves the bare letter. This is exactly what
    the MRZ does by ICAO transliteration (it carries no accents), so folding makes the recto
    comparable to it — without it 'Ê' was dropped entirely and 'GAÊLLE' became 'GALLE'."""
    return "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(char)
    )


def strict_identity_key(value: str | None) -> str:
    """Fold accents, upper-case, drop everything that is not A-Z0-9."""
    return re.sub(r"[^A-Z0-9]", "", _fold_accents(value).upper()) if value else ""
