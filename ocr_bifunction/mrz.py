"""French national ID MRZ — parse + ICAO 7-3-1 check digits.

Two coexisting formats: the pre-2021 French legacy MRZ (2 lines x 36, non-ICAO
field layout — the `mrz` PyPI lib does NOT cover it) and the 2021 electronic eID
(ICAO TD1, 3 x 30). Field positions verified against Wikipedia.

Parsing is LENIENT (role-based, not strict position validation): on real photos the
densest MRZ line — birth date / expiry / sex, full of check digits — is the first to
be lost to a bad angle, while the document-number and name lines survive. We still
want those keys. A FAILED check digit is the real "→ human" signal; the per-field
checks say which keys to trust (unlike RapidOCR's per-line score — the verso scored
0.93 while being garbage).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ocr_bifunction.reader import TextLine

_WEIGHTS = (7, 3, 1)


def icao_check_digit(text: str) -> int:
    """ICAO 9303 check digit over `text` (weights 7,3,1 ; 0-9, A-Z=10..35, '<'=0)."""
    total = 0
    for index, character in enumerate(text):
        if character.isdigit():
            value = int(character)
        elif "A" <= character <= "Z":
            value = ord(character) - ord("A") + 10
        else:  # '<' filler and any unexpected char count as 0
            value = 0
        total += value * _WEIGHTS[index % 3]
    return total % 10


@dataclass
class MrzFields:
    mrz_format: str  # "french_2line" | "td1"
    surname: str | None = None
    given_names: str | None = None
    birth_date: str | None = None  # ISO YYYY-MM-DD, best effort
    sex: str | None = None
    document_number: str | None = None
    checks: dict[str, bool] = field(default_factory=dict)
    raw_lines: tuple[str, ...] = ()


def _normalize_mrz(text: str) -> str:
    # OCR routinely confuses the MRZ filler '<' with '>'; canonicalize to '<'.
    return re.sub(r"\s", "", text.upper()).replace(">", "<")


def extract_mrz_lines(lines: list[TextLine]) -> list[str] | None:
    """Find MRZ lines among OCR lines: uppercase [A-Z0-9<], >= 25 chars, some filler."""
    candidates = []
    for line in sorted(lines, key=lambda item: item.bbox[1]):
        normalized = _normalize_mrz(line.text)
        if (
            len(normalized) >= 25
            and "<" in normalized
            and re.fullmatch(r"[A-Z0-9<]+", normalized)
        ):
            candidates.append(normalized)
    return candidates if len(candidates) >= 2 else None


def _clean_names(raw_field: str) -> str:
    return " ".join(part for part in raw_field.replace("<", " ").split() if part)


def _yymmdd_to_iso(yymmdd: str) -> str | None:
    if not (len(yymmdd) == 6 and yymmdd.isdigit()):
        return None
    year_two_digits = int(yymmdd[0:2])
    century = 1900 if year_two_digits > 25 else 2000  # birthdates: rough past pivot
    return f"{century + year_two_digits:04d}-{yymmdd[2:4]}-{yymmdd[4:6]}"


def parse_mrz(mrz_lines: list[str]) -> MrzFields:
    """Dispatch by line length: ~36 = French legacy 2-line, ~30 = ICAO TD1."""
    if max(len(line) for line in mrz_lines) >= 34:
        return parse_french_2line(mrz_lines[0], mrz_lines[1])
    return parse_td1(mrz_lines)


def parse_french_2line(line1: str, line2: str) -> MrzFields:
    """Parse the French legacy 2x36 MRZ. Positions are 0-indexed slices."""
    line1 = (line1 + "<" * 36)[:36]
    line2 = (line2 + "<" * 36)[:36]

    checks: dict[str, bool] = {}
    if line2[12].isdigit():
        checks["administrative"] = icao_check_digit(line2[0:12]) == int(line2[12])
    if line2[33].isdigit():
        checks["birth_date"] = icao_check_digit(line2[27:33]) == int(line2[33])
    if line2[35].isdigit():
        checks["composite"] = icao_check_digit(line1[0:36] + line2[0:35]) == int(
            line2[35]
        )

    return MrzFields(
        mrz_format="french_2line",
        surname=_clean_names(line1[5:30]) or None,
        given_names=_clean_names(line2[13:27]) or None,
        birth_date=_yymmdd_to_iso(line2[27:33]),
        sex=line2[34] if line2[34] in ("M", "F") else None,
        checks=checks,
        raw_lines=(line1, line2),
    )


def parse_td1(mrz_lines: list[str]) -> MrzFields:
    """Parse ICAO TD1 (3 x 30) leniently — tolerate a missing/garbled middle line."""
    fields = MrzFields(mrz_format="td1", raw_lines=tuple(mrz_lines))

    document_line = next((line for line in mrz_lines if line[:2] in ("ID", "I<")), None)
    name_line = next(
        (line for line in mrz_lines if line is not document_line and "<<" in line), None
    )
    numeric_line = next(
        (
            line
            for line in mrz_lines
            if line not in (document_line, name_line) and line[:6].isdigit()
        ),
        None,
    )

    if document_line is not None:
        document_line = (document_line + "<" * 30)[:30]
        fields.document_number = document_line[5:14].replace("<", "") or None
        if document_line[14].isdigit():
            fields.checks["document_number"] = icao_check_digit(
                document_line[5:14]
            ) == int(document_line[14])
    if name_line is not None:
        parts = [part for part in name_line.split("<<") if part.strip("<")]
        if parts:
            fields.surname = _clean_names(parts[0]) or None
        if len(parts) > 1:
            fields.given_names = _clean_names(parts[1]) or None
    if numeric_line is not None:
        numeric_line = (numeric_line + "<" * 30)[:30]
        fields.birth_date = _yymmdd_to_iso(numeric_line[0:6])
        fields.sex = numeric_line[7] if numeric_line[7] in ("M", "F") else None
        if numeric_line[6].isdigit():
            fields.checks["birth_date"] = icao_check_digit(numeric_line[0:6]) == int(
                numeric_line[6]
            )

    return fields
