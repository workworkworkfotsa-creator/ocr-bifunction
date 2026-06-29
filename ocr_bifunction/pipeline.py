"""End-to-end CI pipeline — one recto+verso pair -> one self-validating record + verdict.

This is the single entry point the demo scripts were rehearsing piecemeal. It wires the
proven stages into one cascade:

    recto  -> read -> match template -> extract fields            (stage ①②③)
    verso  -> read MRZ, raw-first then enhance-retry              (the confidence "pont")
    record -> reconcile shared keys + MRZ check digits -> verdict (stage ② cross-val)

The verso read is the dual-model bridge in miniature: try the cheap raw path first,
escalate to the heavier enhance path ONLY when the cheap one is not trustworthy (MRZ
missing or a check digit failed). A passing raw read never pays for enhancement.

The output `CiRecord` carries both the data product (the consolidated identity fields)
and the verdict envelope (auto/human + the reasons that routed it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ocr_bifunction.mrz import MrzFields, extract_mrz_lines, parse_mrz
from ocr_bifunction.preprocess import EnhancePreprocessor
from ocr_bifunction.reader import OcrEngine, read_document
from ocr_bifunction.reconcile import KEY_MAP, reconcile
from ocr_bifunction.template import extract_fields, load_templates, match_template


@dataclass
class CiRecord:
    """The data product (consolidated identity) plus its verdict envelope."""

    fields: dict[str, str | None]  # merged recto + MRZ identity — the data product
    verdict: str  # "auto" | "human"
    reasons: list[str] = field(default_factory=list)
    template_id: str | None = None
    mrz_format: str | None = None
    verso_read_path: str = "none"  # which read won: "raw"|"enhance"|"escalation"|"none"
    key_matches: dict[str, bool] = field(default_factory=dict)
    failed_checks: list[str] = field(default_factory=list)


def _mrz_is_trustworthy(mrz: MrzFields | None) -> bool:
    """Trust an MRZ only when at least one check digit is present AND all of them pass.

    No checks at all = nothing to anchor on (the verso scored 0.93 while being garbage),
    so an MRZ with zero checks is NOT trustworthy and still escalates to enhancement.
    """
    return bool(mrz and mrz.checks and all(mrz.checks.values()))


def _passing_check_count(mrz: MrzFields | None) -> int:
    return sum(1 for passed in mrz.checks.values() if passed) if mrz else 0


def _read_mrz(engine: OcrEngine, image_bytes: bytes) -> MrzFields | None:
    """Recognize an image and parse its MRZ, or None if no MRZ lines surface."""
    mrz_lines = extract_mrz_lines(engine.recognize(image_bytes))
    return parse_mrz(mrz_lines) if mrz_lines else None


def _better_read(
    current: MrzFields | None,
    current_path: str,
    candidate: MrzFields | None,
    candidate_path: str,
) -> tuple[MrzFields | None, str]:
    """Keep the read with MORE passing check digits; a tie stays on the cheaper `current`."""
    if candidate is None:
        return current, current_path
    if current is None:
        return candidate, candidate_path
    if _passing_check_count(candidate) > _passing_check_count(current):
        return candidate, candidate_path
    return current, current_path


def read_verso_mrz(
    verso_path: Path,
    engine: OcrEngine,
    escalation_engine: OcrEngine | None = None,
) -> tuple[MrzFields | None, str]:
    """Read the verso MRZ in tiers, escalating only when a cheaper tier is not trusted.

    Cascade: raw -> enhance -> (optional) escalation_engine. Returns (mrz, read_path) where
    read_path is "raw" | "enhance" | "escalation" | "none". Each tier keeps whichever read
    has MORE passing check digits; a tie stays on the cheaper tier. The heavy escalation
    engine (a VLM) runs ONLY when raw+enhance yield no trustworthy MRZ AND one is injected —
    the API fast-path passes None and never pays the ~171 s/img cost.
    """
    raw_bytes = verso_path.read_bytes()
    raw_mrz = _read_mrz(engine, raw_bytes)
    if _mrz_is_trustworthy(raw_mrz):
        return raw_mrz, "raw"

    # Cheap path missing or a check failed -> retry with enhancement.
    enhanced_mrz = _read_mrz(engine, EnhancePreprocessor().process(raw_bytes))
    best_mrz, best_path = _better_read(raw_mrz, "raw", enhanced_mrz, "enhance")
    if _mrz_is_trustworthy(best_mrz) or escalation_engine is None:
        return (best_mrz, best_path) if best_mrz is not None else (None, "none")

    # Both cheap tiers exhausted and an escalation engine is wired in -> heavy VLM retry.
    escalated_mrz = _read_mrz(escalation_engine, raw_bytes)
    best_mrz, best_path = _better_read(best_mrz, best_path, escalated_mrz, "escalation")
    return (best_mrz, best_path) if best_mrz is not None else (None, "none")


def read_recto_fields(
    recto_path: Path,
    engine: OcrEngine,
    templates_directory: Path,
    category: str | None = None,
) -> tuple[str | None, dict[str, str | None] | None]:
    """Read the recto, match a template and rebuild its fields. None if no template.

    `category` scopes template matching to one document type (e.g. "carte_identite"):
    when the upload field already knows the type, only that category's templates are
    tried. None tries every template (the default).
    """
    result = read_document(recto_path, engine)
    template = match_template(
        result.lines, load_templates(templates_directory, category)
    )
    if template is None:
        return None, None
    return template["template_id"], extract_fields(result.lines, template)


def _consolidate(
    recto_fields: dict[str, str | None] | None, mrz: MrzFields | None
) -> dict[str, str | None]:
    """Merge recto fields with the MRZ: recto is the base, the MRZ backfills gaps + sex."""
    merged: dict[str, str | None] = dict(recto_fields or {})
    if mrz is not None:
        for recto_key, mrz_attribute in KEY_MAP.items():
            if not merged.get(recto_key):
                mrz_value = getattr(mrz, mrz_attribute)
                if mrz_value:
                    merged[recto_key] = mrz_value
        if mrz.sex and not merged.get("sexe"):
            merged["sexe"] = mrz.sex
    return merged


def process_ci_pair(
    recto_path: Path,
    verso_path: Path,
    engine: OcrEngine,
    templates_directory: Path,
    category: str | None = None,
    escalation_engine: OcrEngine | None = None,
) -> CiRecord:
    """Read a CI recto+verso pair and return one consolidated record + auto/human verdict.

    `category` is the optional document-type hint forwarded to recto template matching:
    e.g. "carte_identite" restricts matching to CI templates only. None tries every one.

    `escalation_engine` is the optional HEAVY fallback (a VLM) for the verso MRZ: it runs
    ONLY when raw+enhance fail the value-check. None (the API fast-path) never escalates —
    the doubtful case routes to human, to be escalated asynchronously off the request path.
    """
    template_id, recto_fields = read_recto_fields(
        recto_path, engine, templates_directory, category
    )
    mrz, verso_read_path = read_verso_mrz(verso_path, engine, escalation_engine)
    merged_fields = _consolidate(recto_fields, mrz)

    # Either side missing means no cross-validation is possible -> human, by construction.
    if recto_fields is None or mrz is None:
        reasons: list[str] = []
        if recto_fields is None:
            reasons.append(
                f"recto: no '{category}' template matched"
                if category
                else "recto: no template matched"
            )
        if mrz is None:
            reasons.append("verso: no MRZ parsed (raw and enhance both failed)")
        return CiRecord(
            fields=merged_fields,
            verdict="human",
            reasons=reasons,
            template_id=template_id,
            mrz_format=mrz.mrz_format if mrz else None,
            verso_read_path=verso_read_path,
        )

    reconciliation = reconcile(recto_fields, mrz)
    return CiRecord(
        fields=merged_fields,
        verdict=reconciliation.verdict,
        reasons=reconciliation.reasons,
        template_id=template_id,
        mrz_format=mrz.mrz_format,
        verso_read_path=verso_read_path,
        key_matches=reconciliation.key_matches,
        failed_checks=reconciliation.failed_checks,
    )
