"""D3 -> D2 promotion — the organic-growth transaction: a validated suggestion activates a template.

This is the "promotion" writer of the contrat-bd-destination.md 3-writer table: it READS a validated
D3 suggestion and WRITES an active D2 template. Validating a curated template in the review queue
makes it active in the library, so the SAME layout matches DETERMINISTICALLY next time and the SLM is
not woken for it again — the whole point of the growth loop.

Two shapes of curated template reach promotion (the human curates; the SLM only flagged the doc):
  - a BRAND-NEW template for a layout D2 did not know (the SLM had answered UNKNOWN);
  - a VARIANT derived from an existing base (grow_template_from_base) when the SLM pointed at a known
    id but match_template missed the doc's layout.
Either way the human owns the content; promotion just activates it and closes the D3 suggestion.

The real internal DB does the D2 write + D3 status flip as ONE transaction; the SQLite proxy does them
in sequence (two separate-connection stores) — the seam is what crosses to IT, the atomicity is theirs.
No PII in curated anchors that get committed anywhere: keep them structural (a public repo rule).
"""

from __future__ import annotations

from ocr_bifunction.storage.review_repository import (
    SUGGESTION_VALIDATED,
    ReviewRepository,
)
from ocr_bifunction.storage.template_repository import TemplateRepository


def grow_template_from_base(
    base_template: dict, new_template_id: str, match_anchors: list[str]
) -> dict:
    """Build a NEW template that reuses a base's fields + validation but matches on new anchors.

    Used when the SLM pointed at a known template (its fields/validation fit) yet match_template
    missed the doc's layout: the reviewer mints a variant whose signature is the confirmed anchors,
    keeping the base's extraction and checks. Pure function — the caller decides the id and anchors."""
    return {
        "template_id": new_template_id,
        "category": base_template.get("category"),
        "match": {"all_anchors": list(match_anchors)},
        "fields": base_template.get("fields", []),
        "validation": base_template.get("validation", {}),
    }


def promote_suggestion(
    review_id: int,
    curated_template: dict,
    *,
    template_repository: TemplateRepository,
    review_repository: ReviewRepository,
) -> str:
    """Activate a curated template in D2 and mark the D3 suggestion validated — the promotion step.

    `curated_template` is the human-owned content (a brand-new template, or one from
    grow_template_from_base). Returns the activated template_id. The internal target DB wraps both
    writes in one transaction; here the two proxy stores are written in sequence (D2 then D3)."""
    template_repository.upsert(curated_template, active=True)
    review_repository.set_suggestion_status(review_id, SUGGESTION_VALIDATED)
    return curated_template["template_id"]
