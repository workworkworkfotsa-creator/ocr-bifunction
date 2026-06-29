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
    verso_read_path: str = "none"  # which verso read won: "raw" | "enhance" | "none"
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


def read_verso_mrz(verso_path: Path, engine: OcrEngine) -> tuple[MrzFields | None, str]:
    """Read the verso MRZ raw-first, retrying with enhancement only on the hard case.

    Returns (mrz, read_path) where read_path is "raw" | "enhance" | "none". The retry
    keeps whichever read has MORE passing check digits; a tie or no gain stays on the
    cheaper raw read.
    """
    raw_bytes = verso_path.read_bytes()
    raw_mrz_lines = extract_mrz_lines(engine.recognize(raw_bytes))
    raw_mrz = parse_mrz(raw_mrz_lines) if raw_mrz_lines else None
    if _mrz_is_trustworthy(raw_mrz):
        return raw_mrz, "raw"

    # Cheap path missing or a check failed -> escalate to the enhancement chain.
    enhanced_bytes = EnhancePreprocessor().process(raw_bytes)
    enhanced_mrz_lines = extract_mrz_lines(engine.recognize(enhanced_bytes))
    enhanced_mrz = parse_mrz(enhanced_mrz_lines) if enhanced_mrz_lines else None

    if raw_mrz is None and enhanced_mrz is None:
        return None, "none"
    if _passing_check_count(enhanced_mrz) > _passing_check_count(raw_mrz):
        return enhanced_mrz, "enhance"
    if raw_mrz is not None:
        return raw_mrz, "raw"
    return enhanced_mrz, "enhance"


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
) -> CiRecord:
    """Read a CI recto+verso pair and return one consolidated record + auto/human verdict.

    `category` is the optional document-type hint forwarded to recto template matching:
    e.g. "carte_identite" restricts matching to CI templates only. None tries every one.
    """
    template_id, recto_fields = read_recto_fields(
        recto_path, engine, templates_directory, category
    )
    mrz, verso_read_path = read_verso_mrz(verso_path, engine)
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
