"""The 2-lane router — one entry point that sends a single document to its lane.

CATÉGORISER, made concrete (cf. CLAUDE.md). For ONE document the router asks a single
question — does it match any STRUCTURED template? — and routes accordingly:

    matches a structured template  -> STRUCTURED lane: extract fields + config-driven
                                      validation -> auto / human verdict
    matches none                   -> RAG lane: no extraction, an extractive summary +
                                      an indexable passage count (the human handle)

This unifies what hp_check.py and facture_check.py did per-category and what rag_check.py
did for the leftovers: the structured runners scoped matching to one category and called
anything else an "intruder"; here a non-structured doc (a memo, an article, a dunning
letter) is not an intruder — it is simply RAG-lane material.

Two honesty guards:
  - A template that declares NO `validation` rules cannot be auto-validated as a single
    doc — e.g. an ID card, whose real check is the recto+verso reconcile (process_ci_pair),
    not a per-doc rule. The router routes it STRUCTURED but verdicts `human` (pair flow),
    never a false `auto`.
  - CI pairs keep their own entry point (process_ci_pair); this router is single-document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ocr_bifunction.rag import Summary, chunk_document, summarize_extractive
from ocr_bifunction.reader import OcrEngine, read_document
from ocr_bifunction.template import (
    extract_fields,
    load_templates,
    match_template,
    validate_fields,
)


@dataclass
class RoutedDocument:
    """Where a document was routed and the first product of its lane."""

    source: str
    lane: str  # "structured" | "rag"
    template_id: str | None = None
    category: str | None = None
    verdict: str | None = None  # structured only: "auto" | "human"
    reasons: list[str] = field(default_factory=list)
    fields: dict[str, str | None] = field(default_factory=dict)
    summary: Summary | None = None  # rag only
    chunk_count: int = 0  # rag only


def route_document(
    document_path: Path,
    templates_directory: Path,
    engine: OcrEngine | None = None,
) -> RoutedDocument:
    """Read one document, decide its lane, and return that lane's first product.

    `engine` is used only when the document is image/scanned (born-digital docx/PDF read
    via their text layer and need none). Matching tries EVERY structured category.
    """
    result = read_document(document_path, engine)
    template = match_template(result.lines, load_templates(templates_directory))

    if template is not None:
        return _structured_result(document_path.name, result.lines, template)

    return _rag_result(document_path.name, result.text)


def _structured_result(source: str, lines, template: dict) -> RoutedDocument:
    fields = extract_fields(lines, template)
    validation = template.get("validation") or {}
    if validation.get("required"):
        reasons = validate_fields(fields, validation)
        verdict = "human" if reasons else "auto"
    else:
        # Matched a structured layout with no single-doc rules (e.g. an ID card): its
        # real validation is the recto+verso reconcile, not a per-doc check. Never auto.
        reasons = [
            "structured template matched but has no single-doc validation rules "
            "(e.g. ID card uses the recto+verso pair flow, process_ci_pair)"
        ]
        verdict = "human"
    return RoutedDocument(
        source=source,
        lane="structured",
        template_id=template["template_id"],
        category=template.get("category"),
        verdict=verdict,
        reasons=reasons,
        fields=fields,
    )


def _rag_result(source: str, text: str) -> RoutedDocument:
    if not text.strip():
        # No structured match and nothing to read (image-only with no OCR engine wired).
        return RoutedDocument(
            source=source,
            lane="rag",
            reasons=[
                "no structured match and no extractable text (image needs an OCR engine)"
            ],
        )
    chunks = chunk_document(text, source=source)
    return RoutedDocument(
        source=source,
        lane="rag",
        summary=summarize_extractive(text),
        chunk_count=len(chunks),
    )
