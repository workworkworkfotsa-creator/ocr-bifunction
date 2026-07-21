"""The template SUGGESTION lane — deterministic-first; the SLM is woken only as a last resort.

BRIEF-suggestion-template deliverable 2. The ONLY input here is a doc that `match_template` already
FAILED to match — the free deterministic path handles the majority and the model stays asleep. Then,
as a last resort, the SLM proposes a `template_id` from the CLOSED LIST of known ids (an enum DERIVED
from templates/*.json + UNKNOWN) plus the anchors it claims to see; the deterministic layer
RE-VERIFIES those anchors against the OCR text (a hallucinated anchor is rejected).

The SLM PROPOSES, the deterministic layer DISPOSES: no raw suggestion is trusted without mechanical
proof. The output form is guaranteed by a grammar (the closed-list enum makes inventing a name
impossible — proven active by gbnf_diag.py); the model's docility only affects WHICH id it picks. The
SLM never creates a template — it stages a suggestion for the human (D3), who validates (-> promote
to D2). We do not modify template.py/reader/the engines; we branch downstream of "no match".

No PII: prompts and the closed list are structural (template ids + generic instructions).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from ocr_bifunction.llama_transport import post_json, resolve_base_url
from ocr_bifunction.reader import TextLine
from ocr_bifunction.template import (
    extract_fields,
    field_values,
    load_templates,
    validate_fields,
)

# Shared llama-swap transport lives in llama_transport. Model key = an entry in the config.yaml.
_DEFAULT_SUGGESTION_MODEL_KEY = "granite-4.0-h-tiny-Q4_K_M"

# The reserved id the model must return when no known template fits (part of the closed list).
UNKNOWN_TEMPLATE_ID = "UNKNOWN"

# A signature lives at the document head; cap the prompt so the router stays cheap on CPU (BRIEF:
# "-c petit ... sortie courte"). Dense text runs ~1.75-3 chars/token, so this stays well under ctx.
_MAX_OCR_CHARACTERS = 4000
# The first call may lazy-load the model on CPU (~100 s for granite); later calls are seconds.
_REQUEST_TIMEOUT_SECONDS = 300.0


@dataclass
class SuggestionOutcome:
    """The suggestion lane's conclusion for one doc that matched no template — TWO deterministic
    gates dispose of what the model PROPOSES:

    1. anti-hallucination — `confirmed_anchors` = the proposed anchors actually present in the OCR
       (the model claims to see them; we check). No confirmed anchor -> the model made it up.
    2. fit — `validation_reasons` from actually TRYING the proposed template (extract_fields +
       validate_fields), exactly the brief's "anchors confirmed -> TRY that template". A confirmed
       anchor only proves the model copied real text; only the fit test proves the doc BELONGS.

    `verified` is True iff a KNOWN id was proposed, at least one anchor re-verified, AND the tried
    template validated (no reasons). Anything else -> human."""

    suggested_template_id: str | None
    proposed_anchors: list[str] = field(default_factory=list)
    confirmed_anchors: list[str] = field(default_factory=list)
    validation_reasons: list[str] = field(default_factory=list)
    tried: bool = False  # did the anchors pass the hallucination gate, so we ran extract+validate?
    verified: bool = False


def known_template_ids(
    templates_directory: Path, category: str | None = None
) -> list[str]:
    """The closed list: every existing template_id (optionally scoped to one category)."""
    return [
        template["template_id"]
        for template in load_templates(templates_directory, category)
        if template.get("template_id")
    ]


def suggestion_json_schema(template_ids: list[str]) -> dict:
    """The constrained output shape, DERIVED from the templates: template_id is an enum of the
    known ids + UNKNOWN (the model cannot invent a name), anchors is a short list of strings."""
    return {
        "type": "object",
        "properties": {
            "template_id": {
                "type": "string",
                "enum": [*template_ids, UNKNOWN_TEMPLATE_ID],
            },
            "anchors": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        },
        "required": ["template_id", "anchors"],
    }


def _build_prompt(ocr_text: str, template_ids: list[str]) -> str:
    id_list = ", ".join(template_ids) or "(none)"
    return (
        "You classify a scanned document by matching it to a KNOWN template id.\n"
        f"Known template ids: {id_list}.\n"
        'Return JSON {"template_id": <one id above, or "UNKNOWN">, "anchors": '
        "[<short phrases COPIED VERBATIM from the text that justify your choice>]}.\n"
        "Choose UNKNOWN if none fits. Never invent an anchor — copy it from the text.\n\n"
        f"DOCUMENT TEXT:\n{ocr_text[:_MAX_OCR_CHARACTERS]}\n\nJSON:"
    )


def request_suggestion(
    ocr_text: str,
    template_ids: list[str],
    *,
    base_url: str | None = None,
    model_key: str | None = None,
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
) -> dict:
    """POST the OCR text to llama-swap with a schema-constrained grammar; return the parsed JSON.

    The `json_schema` body field makes llama-server derive a GBNF grammar from the enum+array
    schema, so the reply is always valid JSON of that shape (no free-text parsing). Uses
    /completion (a one-shot router, no jinja) per the brief."""
    base = resolve_base_url(base_url)
    model = model_key or os.environ.get(
        "SUGGESTION_MODEL_KEY", _DEFAULT_SUGGESTION_MODEL_KEY
    )
    payload = post_json(
        base,
        "/completion",
        {
            "model": model,
            "prompt": _build_prompt(ocr_text, template_ids),
            "json_schema": suggestion_json_schema(template_ids),
            "temperature": 0.0,
            "n_predict": 256,
            "cache_prompt": True,
        },
        timeout=timeout,
        server_label="suggestion",
    )
    return json.loads(payload.get("content", "{}"))


def verify_anchors(ocr_text: str, anchors: list[str]) -> list[str]:
    """Deterministic re-verification: keep only anchors that are literally present in the OCR text
    (case-insensitive). This defeats a hallucinated justification — the model claimed to see them,
    and we check. Returns the confirmed subset."""
    haystack = ocr_text.lower()
    return [
        anchor
        for anchor in anchors
        if anchor.strip() and anchor.strip().lower() in haystack
    ]


def suggest_template(
    ocr_text: str,
    lines: list[TextLine],
    templates_directory: Path,
    *,
    category: str | None = None,
    base_url: str | None = None,
    model_key: str | None = None,
    templates: list[dict] | None = None,
) -> SuggestionOutcome:
    """Wake the SLM for a doc that matched no template, then DISPOSE of its answer deterministically
    through the brief's two gates: anti-hallucination (proposed anchors present in the OCR) and FIT
    (try the proposed template — extract_fields + validate_fields).

    `verified` only when a KNOWN id was proposed, an anchor re-verified, AND the tried template
    validated. UNKNOWN / out-of-list id / all-hallucinated anchors / validation failure -> human.
    `lines` (geometry) is needed to actually try the template. The caller runs `match_template`
    FIRST and only calls this on a miss.

    `templates` injects the template list directly (the D2 store read path, mirroring
    route_document) instead of loading the JSON files; `category` scoping applies either way —
    the closed list the model picks from must be the SAME list the deterministic match used."""
    if templates is None:
        templates = load_templates(templates_directory, category)
    elif category is not None:
        templates = [
            template for template in templates if template.get("category") == category
        ]
    template_ids = [
        template["template_id"] for template in templates if template.get("template_id")
    ]
    reply = request_suggestion(
        ocr_text, template_ids, base_url=base_url, model_key=model_key
    )
    proposed_id = reply.get("template_id")
    proposed_anchors = [
        str(anchor) for anchor in reply.get("anchors", []) if str(anchor).strip()
    ]
    # Gate 0: the model must name a KNOWN id (the grammar enforces this, but re-check defensively).
    if proposed_id == UNKNOWN_TEMPLATE_ID or proposed_id not in template_ids:
        return SuggestionOutcome(None, proposed_anchors)
    # Gate 1 (anti-hallucination): at least one proposed anchor must be really in the text.
    confirmed = verify_anchors(ocr_text, proposed_anchors)
    if not confirmed:
        return SuggestionOutcome(proposed_id, proposed_anchors, [])
    # Gate 2 (fit): TRY the proposed template — a confirmed anchor only proves the model copied real
    # text; only extract + validate proves the doc actually BELONGS to this template.
    template = next(
        template for template in templates if template["template_id"] == proposed_id
    )
    validation = template.get("validation") or {}
    if not validation.get("required"):
        # No single-doc rules to prove fit (e.g. an ID card, validated by the pair flow) -> human.
        return SuggestionOutcome(
            proposed_id,
            proposed_anchors,
            confirmed,
            validation_reasons=[
                "suggested template has no single-doc validation rules"
            ],
            tried=True,
        )
    reasons = validate_fields(field_values(extract_fields(lines, template)), validation)
    return SuggestionOutcome(
        suggested_template_id=proposed_id,
        proposed_anchors=proposed_anchors,
        confirmed_anchors=confirmed,
        validation_reasons=reasons,
        tried=True,
        verified=not reasons,
    )
