"""D-c (part 1) — the constrained SLM NAMES a deterministic draft's placeholder fields.

BRIEF-template-drafting deliverable D-c. `draft_from_cluster` (D-b) produces a draft whose
fields carry DETERMINISTIC placeholder names (label slugs like `nom_du_titulaire`). This
module wakes the shared SLM to propose a clearer SEMANTIC name per field; the deterministic
layer DISPOSES of the proposal (sanitize to a safe identifier, keep names unique, fall back
to the placeholder on garbage) and the renamed draft is RE-TESTED unchanged on the whole
cluster — a rename that broke extraction (it cannot, being a pure relabel) is rejected and
the original draft is kept.

The SLM PROPOSES, the deterministic layer DISPOSES. The output shape is guaranteed by a
json_schema grammar (each entry's `placeholder` is an enum of the draft's OWN field names,
so the model cannot rename a field that does not exist); only the free-text `name` is the
model's, and it is sanitized before it ever touches the draft.

No PII: the prompt sends only the STRUCTURAL field placeholders (already label slugs) and
their extraction method, NEVER the extracted values (which are the holder's personal data).
Same discipline as suggestion.py — prompts are structural. This slice does the brief's
"NOMMER les champs"; proposing normalize/pattern and the anti-fraud CHECK candidates waits
on the check kit those candidates would be re-tested against (not yet coded).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field

from ocr_bifunction.drafting import DraftingDocument
from ocr_bifunction.llama_transport import post_json, resolve_base_url
from ocr_bifunction.template import (
    extract_fields,
    match_template,
    validate_fields,
)

# Shared llama-swap transport lives in llama_transport (same convention as suggestion.py).
_DEFAULT_NAMING_MODEL_KEY = "granite-4.0-h-tiny-Q4_K_M"
# First call may lazy-load the model on CPU (~100 s for granite); later calls are seconds.
_REQUEST_TIMEOUT_SECONDS = 300.0

# A field name must be a safe, lowercase identifier; keep only real word characters.
_NAME_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_MAXIMUM_NAME_LENGTH = 40


@dataclass
class NamedDraftReport:
    """Outcome of naming one draft's fields.

    `template` is the RENAMED draft when the rename re-tested green on the whole cluster,
    otherwise the ORIGINAL draft unchanged (a safe fallback, with a reason). `name_mapping`
    is placeholder -> final field name (identity for anything the model left untouched or
    that was rejected). `reasons` records why a rename was refused, if any."""

    template: dict
    name_mapping: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


def draft_placeholder_names(draft: dict) -> list[str]:
    """The draft's own field names — the closed list the model may rename (an enum)."""
    return [field_entry["name"] for field_entry in draft.get("fields", [])]


def field_naming_json_schema(placeholder_names: list[str]) -> dict:
    """Constrained shape: one entry per field the model chooses to rename; `placeholder`
    is an enum of the draft's OWN names (it cannot name a field that does not exist),
    `name` is a free string the deterministic layer sanitizes afterwards."""
    return {
        "type": "object",
        "properties": {
            "field_names": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "placeholder": {"type": "string", "enum": placeholder_names},
                        "name": {"type": "string"},
                    },
                    "required": ["placeholder", "name"],
                },
            }
        },
        "required": ["field_names"],
    }


def _field_method(field_entry: dict) -> str:
    if "pattern" in field_entry:
        return "regex over a colon line (label : value)"
    direction = field_entry.get("direction", "?")
    return f"geometry: the value {direction} a label"


def _build_prompt(draft: dict) -> str:
    lines = [
        f"- {field_entry['name']}  ({_field_method(field_entry)})"
        for field_entry in draft.get("fields", [])
    ]
    category = draft.get("category", "document")
    return (
        "You give clear field names to a document-extraction template.\n"
        f"Document category: {category}.\n"
        "Each field below has a placeholder name derived from its label. Propose a "
        "concise, lowercase snake_case name that names the SEMANTIC ROLE of the field "
        "(for example a holder's name, an issue date, an expiry date, a qualification "
        "code). Keep the placeholder if it is already clear.\n"
        'Return JSON {"field_names": [{"placeholder": <one name below>, "name": '
        "<snake_case name>}]} with one entry per field.\n\n"
        "FIELDS:\n" + "\n".join(lines) + "\n\nJSON:"
    )


def request_field_names(
    draft: dict,
    *,
    base_url: str | None = None,
    model_key: str | None = None,
    timeout: float = _REQUEST_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """POST the draft's STRUCTURAL field list to llama-swap with a schema-constrained
    grammar; return {placeholder -> proposed raw name}. Uses /completion (a one-shot
    router, no jinja) like suggestion.py."""
    placeholder_names = draft_placeholder_names(draft)
    if not placeholder_names:
        return {}
    base = resolve_base_url(base_url)
    model = model_key or os.environ.get("NAMING_MODEL_KEY", _DEFAULT_NAMING_MODEL_KEY)
    payload = post_json(
        base,
        "/completion",
        {
            "model": model,
            "prompt": _build_prompt(draft),
            "json_schema": field_naming_json_schema(placeholder_names),
            "temperature": 0.0,
            "n_predict": 256,
            "cache_prompt": True,
        },
        timeout=timeout,
        server_label="naming",
    )
    reply = json.loads(payload.get("content", "{}"))
    proposed: dict[str, str] = {}
    for entry in reply.get("field_names", []):
        placeholder = str(entry.get("placeholder", ""))
        name = str(entry.get("name", ""))
        if placeholder in placeholder_names and name.strip():
            proposed[placeholder] = name
    return proposed


def _sanitize_name(raw_name: str) -> str:
    """Fold to ASCII and slugify to a safe lowercase identifier; empty on garbage."""
    folded = (
        unicodedata.normalize("NFKD", raw_name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return _NAME_SLUG_PATTERN.sub("_", folded.lower()).strip("_")[:_MAXIMUM_NAME_LENGTH]


def apply_field_names(
    draft: dict, proposed: dict[str, str]
) -> tuple[dict, dict[str, str]]:
    """Pure: rename the draft's fields (and its `validation.required` entries) from the
    model's proposal. Each proposed name is sanitized to a safe identifier; an empty or
    duplicate result falls back to the placeholder, so the mapping is always total and
    collision-free. Returns (renamed_draft, placeholder -> final name)."""
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for field_entry in draft.get("fields", []):
        placeholder = field_entry["name"]
        candidate = _sanitize_name(proposed.get(placeholder, ""))
        final = candidate if candidate and candidate not in taken else placeholder
        # A collision with an already-assigned name also falls back to the placeholder
        # (placeholders are unique by construction in D-b).
        if final in taken:
            final = placeholder
        mapping[placeholder] = final
        taken.add(final)

    renamed = json.loads(json.dumps(draft))  # deep copy, draft is plain JSON
    for field_entry in renamed.get("fields", []):
        field_entry["name"] = mapping[field_entry["name"]]
    for rule in renamed.get("validation", {}).get("required", []):
        if rule.get("field") in mapping:
            rule["field"] = mapping[rule["field"]]
    return renamed, mapping


def _draft_retests_green(draft: dict, cluster: list[DraftingDocument]) -> list[str]:
    """Re-run the D-b gate on a draft: it must match, extract every required field, and
    validate on EVERY cluster document. Returns the failure reasons ([] when green)."""
    validation = draft.get("validation") or {}
    reasons: list[str] = []
    for document in cluster:
        if match_template(document.lines, [draft]) is None:
            reasons.append(f"{document.source}: renamed draft no longer matches")
            continue
        extracted = extract_fields(document.lines, draft)
        reasons.extend(
            f"{document.source}: {failure}"
            for failure in validate_fields(extracted, validation)
        )
    return reasons


def name_draft_fields(
    draft: dict,
    cluster: list[DraftingDocument],
    *,
    base_url: str | None = None,
    model_key: str | None = None,
) -> NamedDraftReport:
    """Wake the SLM to name the draft's placeholder fields, then DISPOSE deterministically:
    sanitize each proposal, keep names unique, and RE-TEST the renamed draft on the whole
    cluster (the D-b gate, unchanged). Green -> the renamed draft; any failure -> the
    ORIGINAL draft with a reason (a pure relabel cannot break extraction, so a failure is a
    surprise worth surfacing rather than silently shipping)."""
    proposed = request_field_names(draft, base_url=base_url, model_key=model_key)
    if not proposed:
        return NamedDraftReport(
            template=draft,
            name_mapping={name: name for name in draft_placeholder_names(draft)},
            reasons=["model proposed no field name"],
        )
    renamed, mapping = apply_field_names(draft, proposed)
    reasons = _draft_retests_green(renamed, cluster)
    if reasons:
        return NamedDraftReport(
            template=draft,
            name_mapping={name: name for name in draft_placeholder_names(draft)},
            reasons=["renamed draft failed re-test, kept original", *reasons],
        )
    return NamedDraftReport(template=renamed, name_mapping=mapping)
