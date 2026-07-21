"""Text-integrity WIRING smoke — the guard câblé into the read -> route seam.

    uv run python text_integrity_wiring_smoke.py

Companion to text_integrity_guard_smoke.py (which proves the guard's LOGIC in isolation). This
one proves the guard is actually PLUGGED IN: that a corrupted read reaches the router, names
itself in the reasons, and — the load-bearing behaviour — can never auto-validate.

END-TO-END without a broken-CMap PDF: the reproducer is a synthetic PDF whose TEXT LAYER
literally carries the mojibake characters (PyMuPDF `insert_text`). The guard reads the extracted
string, so how the string got corrupted is irrelevant to it — writing the garbage directly is
observationally identical to a broken ToUnicode CMap producing it, and costs no heavy run.
PII-free, no OCR engine, no Docling.

Proves:
  1. e2e READ: a mojibake PDF -> disposition "repairable_mojibake", and the repair candidate
     round-trips to the EXACT original (ftfy even recovers the nbsp flattened by PDF extraction);
  2. e2e ROUTE (RAG lane): the reason surfaces on the RoutedDocument, and the repair is named as
     a SUGGESTION, never applied;
  3. e2e NO FALSE POSITIVE: a clean accented PDF adds no integrity reason through the whole path;
  4. the structured lane can never AUTO on corrupted characters (AUTO -> REVIEW);
  5. a REJECT is never softened by the integrity signal (reject > review > auto stands);
  6. a clean assessment is a strict no-op;
  7. irreversible loss (U+FFFD) offers NO repair suggestion — nothing can be brought back.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf

from ocr_bifunction.reader import read_document
from ocr_bifunction.router import (
    RoutedDocument,
    apply_text_integrity_signal,
    route_document,
)
from ocr_bifunction.text_integrity_guard import assess_text_integrity
from ocr_bifunction.verdict import Verdict

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _write_pdf(path: Path, text: str) -> None:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text, fontsize=11)
    document.save(path)
    document.close()


def run() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="ocr_bifunction_integrity_wiring_"))
    empty_templates = scratch / "templates"
    empty_templates.mkdir()

    original = "été à Noël rapport annuel des travaux"
    mojibake = original.encode("utf-8").decode("latin-1")

    mojibake_pdf = scratch / "mojibake.pdf"
    clean_pdf = scratch / "clean.pdf"
    _write_pdf(mojibake_pdf, mojibake)
    _write_pdf(clean_pdf, original)

    # 1. e2e READ: the corruption is detected on the extracted text, whatever produced it.
    read = read_document(mojibake_pdf)
    _check(
        "e2e read: mojibake PDF -> repairable_mojibake, repair round-trips to the exact original",
        read.text_integrity is not None
        and read.text_integrity.disposition == "repairable_mojibake"
        and read.text_integrity.repaired_text is not None
        and read.text_integrity.repaired_text.strip() == original,
    )

    # 2. e2e ROUTE: the reason reaches the routed document, repair named as a suggestion.
    routed = route_document(mojibake_pdf, empty_templates)
    _check(
        "e2e route: the mojibake reason surfaces on the routed document",
        any("mojibake" in reason for reason in routed.reasons),
    )
    _check(
        "e2e route: the repair is offered as a SUGGESTION, never applied",
        any("suggestion" in reason for reason in routed.reasons),
    )

    # 3. e2e NO FALSE POSITIVE: clean accented text stays silent through the whole path.
    clean_read = read_document(clean_pdf)
    clean_routed = route_document(clean_pdf, empty_templates)
    _check(
        "e2e clean accented PDF -> clean, no integrity reason added anywhere",
        clean_read.text_integrity is not None
        and clean_read.text_integrity.disposition == "clean"
        and not any("mojibake" in reason for reason in clean_routed.reasons),
    )

    # 4. THE load-bearing rule: corrupted characters can never auto-validate.
    corrupted = assess_text_integrity(mojibake)
    auto_document = RoutedDocument(
        source="x", lane="structured", verdict=Verdict.AUTO, reasons=[]
    )
    apply_text_integrity_signal(auto_document, corrupted)
    _check(
        "structured lane: AUTO is escalated to REVIEW on corrupted characters",
        auto_document.verdict is Verdict.REVIEW and bool(auto_document.reasons),
    )

    # 5. A proven-invalid document is never softened back to review.
    reject_document = RoutedDocument(
        source="x",
        lane="structured",
        verdict=Verdict.REJECT,
        reasons=["proven invalid"],
    )
    apply_text_integrity_signal(reject_document, corrupted)
    _check(
        "REJECT is never softened by the integrity signal (reject > review stands)",
        reject_document.verdict is Verdict.REJECT,
    )

    # 6. Clean assessment must not touch anything.
    untouched = RoutedDocument(
        source="x", lane="structured", verdict=Verdict.AUTO, reasons=[]
    )
    apply_text_integrity_signal(untouched, assess_text_integrity(original))
    _check(
        "clean assessment is a strict no-op (AUTO stays AUTO, no reason added)",
        untouched.verdict is Verdict.AUTO and untouched.reasons == [],
    )

    # 7. Irreversible loss offers no repair — destroyed bytes stay destroyed.
    lost_document = RoutedDocument(source="x", lane="rag", verdict=None, reasons=[])
    apply_text_integrity_signal(lost_document, assess_text_integrity("abc�def rapport"))
    _check(
        "irreversible loss: flagged, but NO repair suggestion offered",
        any("irreversible" in reason for reason in lost_document.reasons)
        and not any("suggestion" in reason for reason in lost_document.reasons),
    )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT text-integrity wiring (read -> route -> verdict): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
