"""Text-integrity observer — the guard run READ-ONLY over the real corpus.

    uv run python text_integrity_observer_run.py
    uv run python text_integrity_observer_run.py --inputs inputs/cplx --limit 5

What this actually answers (and what it does NOT): the guard has NO badness threshold — its
dispositions rest on booleans (`U+FFFD` present, `ftfy.badness.is_bad`). So this pass is FIRST a
FALSE-POSITIVE AUDIT on real documents: does the heuristic fire on genuinely clean born-digital
files? Every hit here is a document the wiring would now escalate from `auto` to `review`, so a hit
on a clean file is a REGRESSION IN DISGUISE, not a discovery. The badness distribution is the
secondary product, kept for the day a numeric threshold is actually wanted.

LIGHT BY CONSTRUCTION — safe on the shared machine: no OCR engine and no heavy page converter are
wired in, so born-digital PDFs are read through the PyMuPDF text layer in milliseconds and
image-only pages are simply skipped (reported as "no text") rather than triggering an OCR run.
Nothing here competes with another project for RAM.

PII: prints ONLY metadata (character counts, disposition, scores) and NEVER document content. File
NAMES are redacted by default too — this walks all of `inputs/`, where a scan's filename can itself
carry a person's name (unlike the contract corpus). Pass `--names` for a local run when you need to
identify a flagged file; never paste that output anywhere.

This script MEASURES, it does not verdict: the retained interpretation is the user's call.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from ocr_bifunction.reader import read_document

DEFAULT_INPUT_DIRECTORY = Path("inputs")
READABLE_SUFFIXES = {".pdf", ".docx"}


def _display_name(document_path: Path, index: int, *, show_real_names: bool) -> str:
    """Redacted by default: a filename under `inputs/` can itself carry a person's name, so the
    identity shown is a positional placeholder plus the parent folder and suffix — enough to locate
    a flagged file by re-running with `--names` locally, without printing the name here."""
    if show_real_names:
        return document_path.name
    return f"{document_path.parent.name}/doc{index:02d}{document_path.suffix.lower()}"


def _collect_documents(input_paths: list[Path], limit: int | None) -> list[Path]:
    documents: list[Path] = []
    for input_path in input_paths:
        if input_path.is_dir():
            documents.extend(
                sorted(
                    path
                    for path in input_path.rglob("*")
                    if path.is_file() and path.suffix.lower() in READABLE_SUFFIXES
                )
            )
        elif input_path.is_file() and input_path.suffix.lower() in READABLE_SUFFIXES:
            documents.append(input_path)
    return documents[:limit] if limit is not None else documents


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="*", default=[str(DEFAULT_INPUT_DIRECTORY)])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--names",
        action="store_true",
        help="show real file names (LOCAL use only: a filename can carry a person's name)",
    )
    arguments = parser.parse_args()

    documents = _collect_documents([Path(p) for p in arguments.inputs], arguments.limit)
    if not documents:
        print(f"No readable document under {arguments.inputs} ({READABLE_SUFFIXES}).")
        return 1

    print(f"Observing {len(documents)} document(s) — light read, no OCR, no Docling.\n")
    print(f"{'document':<44} {'chars':>7} {'badness':>8}  disposition")
    print("-" * 82)

    dispositions: Counter[str] = Counter()
    clean_badness_scores: list[int] = []
    flagged: list[tuple[str, str, int, int]] = []

    for index, document_path in enumerate(documents, start=1):
        result = read_document(document_path)
        name = _display_name(document_path, index, show_real_names=arguments.names)
        display_name = name if len(name) <= 43 else name[:40] + "..."
        assessment = result.text_integrity

        if assessment is None:
            dispositions["(no text — needs OCR, not assessed)"] += 1
            print(f"{display_name:<44} {'-':>7} {'-':>8}  (no text — not assessed)")
            continue

        dispositions[assessment.disposition] += 1
        if assessment.disposition == "clean":
            clean_badness_scores.append(assessment.badness_score)
        else:
            flagged.append(
                (
                    name,
                    assessment.disposition,
                    assessment.badness_score,
                    assessment.replacement_character_count,
                )
            )
        print(
            f"{display_name:<44} {assessment.character_count:>7} "
            f"{assessment.badness_score:>8}  {assessment.disposition}"
        )

    print("\n" + "=" * 82)
    print("DISPOSITIONS")
    for disposition, count in dispositions.most_common():
        print(f"  {count:>4}  {disposition}")

    if clean_badness_scores:
        ordered = sorted(clean_badness_scores)
        print(
            f"\nBADNESS on documents judged CLEAN (n={len(ordered)}): "
            f"min={ordered[0]} median={ordered[len(ordered) // 2]} max={ordered[-1]}"
        )
        print(
            "  -> the headroom a numeric threshold would have, IF one is ever wanted. "
            "Nothing gates on this today."
        )

    print("\nFALSE-POSITIVE AUDIT (the point of this pass)")
    if not flagged:
        print(
            "  0 flagged. On this corpus the guard escalates NOTHING that reads clean —\n"
            "  the wiring adds no review load. That is the result we wanted."
        )
    else:
        print(
            f"  {len(flagged)} document(s) would now be escalated auto -> review.\n"
            "  Each MUST be inspected by hand: a genuinely corrupted read is a catch,\n"
            "  a clean one is a false positive and the heuristic needs a guard rail."
        )
        for name, disposition, badness_score, replacement_count in flagged:
            print(
                f"    - {name}: {disposition} (badness={badness_score}, "
                f"U+FFFD={replacement_count})"
            )

    print("\nThis script measures; the interpretation and the decision are the user's.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
