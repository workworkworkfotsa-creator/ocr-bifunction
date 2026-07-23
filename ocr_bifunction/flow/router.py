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

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from datetime import date

from ocr_bifunction.knowledge.rag import Summary, chunk_document, summarize_extractive
from ocr_bifunction.reading.reader import (
    HeavyPageConverterFactory,
    OcrEngine,
    TextLine,
    read_document,
)
from ocr_bifunction.extraction.suggestion import SuggestionOutcome
from ocr_bifunction.reading.text_integrity_guard import TextIntegrityAssessment
from ocr_bifunction.extraction.template import (
    ExtractedField,
    ValidationContext,
    evaluate_validation,
    extract_fields,
    field_values,
    load_templates,
    match_template,
)
from ocr_bifunction.validation.verdict import Verdict

# The suggestion hook: called ONLY on a no-match (the brief's "downstream of pas de match"),
# with the already-read text + lines so the doc is never read/OCR'd twice. (text, lines,
# category) -> outcome. Default None = the hook never fires — the API fast path stays free of
# the SLM (same opt-in pattern as escalation_engine); the batch regime wires it in.
SuggesterHook = Callable[[str, list[TextLine], str | None], SuggestionOutcome | None]


@dataclass
class RoutedDocument:
    """Where a document was routed and the first product of its lane."""

    source: str
    lane: str  # "structured" | "rag"
    template_id: str | None = None
    category: str | None = None
    verdict: Verdict | None = None  # structured only; None for the RAG lane
    reasons: list[str] = field(default_factory=list)
    fields: dict[str, ExtractedField] = field(default_factory=dict)
    summary: Summary | None = None  # rag only
    chunk_count: int = 0  # rag only
    suggestion: SuggestionOutcome | None = None  # rag only, when a suggester hook fired


def route_document(
    document_path: Path,
    templates_directory: Path,
    engine: OcrEngine | None = None,
    category: str | None = None,
    templates: list[dict] | None = None,
    suggester: SuggesterHook | None = None,
    context: ValidationContext | None = None,
    today: date | None = None,
    heavy_page_converter_factory: HeavyPageConverterFactory | None = None,
) -> RoutedDocument:
    """Read one document, decide its lane, and return that lane's first product.

    `engine` is used only when the document is image/scanned (born-digital docx/PDF read
    via their text layer and need none). `category` scopes structured matching to one
    declared document type (e.g. "facture"): a doc declared one type but matching no such
    template falls through to the RAG lane. None tries EVERY structured category.

    `templates` injects the template list directly — the D2 store read path (the worker
    reads ACTIVE templates from `ocr_templates`, cf. contrat-bd-destination.md) — instead
    of loading the committed JSON files; the `category` scoping applies either way.

    `suggester` (opt-in, batch regime) fires ONLY on a no-match with readable text: the SLM
    proposes a template from the closed list, deterministically re-verified downstream. The
    outcome rides on the RoutedDocument; staging it (D3) is the caller's sink concern.

    `context`/`today` feed the anti-fraud validation: the context-dependent checks
    (reconcile_ci / issuer_registry / corroborated_by) need `context`, and date_order's
    freshness side reads `today`. Absent context resolves to REVIEW (never a false reject).
    """
    result = read_document(
        document_path,
        engine,
        heavy_page_converter_factory=heavy_page_converter_factory,
    )
    if templates is None:
        available_templates = load_templates(templates_directory, category)
    else:
        available_templates = [
            template
            for template in templates
            if category is None or template.get("category") == category
        ]
    template = match_template(result.lines, available_templates)

    if template is not None:
        structured = _structured_result(
            document_path.name, result.lines, template, context, today
        )
        return apply_text_integrity_signal(structured, result.text_integrity)

    routed = _rag_result(
        document_path.name,
        result.text,
        missing_pages=result.missing_pages,
        low_form_pages=result.low_form_pages,
    )
    if suggester is not None and result.text.strip():
        routed.suggestion = suggester(result.text, result.lines, category)
    return apply_text_integrity_signal(routed, result.text_integrity)


def apply_text_integrity_signal(
    routed: RoutedDocument, assessment: TextIntegrityAssessment | None
) -> RoutedDocument:
    """Surface a corrupted-characters read on the routed document — and never let it AUTO.

    The character-integrity edge is ORTHOGONAL to the template checks: a document can match its
    template and satisfy every validation rule while the CHARACTERS those fields were extracted
    from are mojibake. So a non-clean assessment adds its specific reason AND escalates an AUTO
    verdict to REVIEW. A REJECT is never softened (reject > review > auto stands), and the RAG
    lane — whose verdict is None and which already routes to a human — only gains the reason.

    The repair, when one exists, rides along as a SUGGESTION named in the reasons; it is NEVER
    applied here. Deciding on the content after detection is the human's call."""
    if assessment is None or assessment.disposition == "clean":
        return routed
    reasons = [*routed.reasons, *assessment.reasons]
    if assessment.repaired_text is not None:
        reasons.append(
            "a repaired reading is available as a suggestion "
            "(human validates, never auto-applied)"
        )
    routed.reasons = reasons
    if routed.verdict is Verdict.AUTO:
        routed.verdict = Verdict.REVIEW
    return routed


def _structured_result(
    source: str,
    lines,
    template: dict,
    context: ValidationContext | None = None,
    today: date | None = None,
) -> RoutedDocument:
    fields = extract_fields(lines, template)
    validation = template.get("validation") or {}
    if validation.get("required"):
        # The verdict engine reasons about VALUES; the provenance rides on `fields` to the
        # record (and D1), so a reviewer can be shown the zone each value was read from.
        outcome = evaluate_validation(
            field_values(fields), validation, today=today, context=context
        )
        verdict = outcome.verdict
        reasons = outcome.reject_reasons + outcome.review_reasons
    else:
        # Matched a structured layout with no single-doc rules (e.g. an ID card): its
        # real validation is the recto+verso reconcile, not a per-doc check. Never auto.
        reasons = [
            "structured template matched but has no single-doc validation rules "
            "(e.g. ID card uses the recto+verso pair flow, process_ci_pair)"
        ]
        verdict = Verdict.REVIEW
    return RoutedDocument(
        source=source,
        lane="structured",
        template_id=template["template_id"],
        category=template.get("category"),
        verdict=verdict,
        reasons=reasons,
        fields=fields,
    )


def _rag_result(
    source: str,
    text: str,
    *,
    missing_pages: list[int] | None = None,
    low_form_pages: list[int] | None = None,
) -> RoutedDocument:
    # A heavy resilient read may have dropped pages (genuinely bad content that survived even the
    # smallest batch) or produced low-form ones: surface WHICH pages, the page-grain twin of the CI
    # `missing: [recto|verso]` reason. The RAG lane already routes to review — these make the reason
    # specific and actionable instead of a generic "routed to review".
    completeness_reasons: list[str] = []
    if missing_pages:
        completeness_reasons.append(
            f"incomplete conversion: {len(missing_pages)} page(s) never produced "
            f"(routed to human review): pages {missing_pages}"
        )
    if low_form_pages:
        completeness_reasons.append(
            "low-form pages (layout below threshold, structure not confidently "
            f"recovered): pages {low_form_pages}"
        )
    if not text.strip():
        # No structured match and nothing to read (image-only with no OCR engine wired).
        return RoutedDocument(
            source=source,
            lane="rag",
            reasons=[
                "no structured match and no extractable text (image needs an OCR engine)",
                *completeness_reasons,
            ],
        )
    chunks = chunk_document(text, source=source)
    return RoutedDocument(
        source=source,
        lane="rag",
        summary=summarize_extractive(text),
        chunk_count=len(chunks),
        reasons=completeness_reasons,
    )
