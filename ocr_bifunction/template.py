"""Stage ②③ — match a category template and rebuild structured fields from geometry.

Raw OCR lines carry no links; their boxes do. A template names, per field, a label
anchor and the spatial rule that ties it to its value ("the value sits below the
label, in the same column"). This is the deterministic Python post-processing the
Backoffice validates — no model, just geometry + rules.

Several templates can exist per category (a CI has many formats); match_template
picks the one whose signature anchors are all present.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ocr_bifunction.reader import TextLine

# The ONE strict identity key (fold accents + uppercase + drop non-alphanumeric). Reused
# on purpose so the anti-fraud name match stays IDENTICAL to the CI reconcile: strict, no
# fuzzy tolerance — "Ahmed" and "Hamed" must not collide (the sibling-fraud core), accent
# folding is the only allowance (cf. memory reconcile-name-match-strict).
from ocr_bifunction.reconcile import _normalize as _strict_identity_key

# Horizontal tolerance (pixels) for "same column": a value counts as below a label
# when their left edges line up within this band. Tuned on ~1100px-wide CI scans.
COLUMN_X_TOLERANCE = 60.0
# Vertical tolerance (pixels) for "same row" (direction "right").
ROW_Y_TOLERANCE = 25.0


def load_templates(directory: Path, category: str | None = None) -> list[dict]:
    """Load every template JSON, optionally keeping only one document category.

    `category` is the optional document-type hint (e.g. "carte_identite"): when the
    caller already knows the upload is a CI, only CI templates are considered — an
    invoice template can never accidentally match, and matching is cheaper. None loads
    every template (the default, category-agnostic behavior).
    """
    templates = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]
    if category is not None:
        templates = [
            template for template in templates if template.get("category") == category
        ]
    return templates


def _normalize_for_match(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _fuzzy_contains(needle: str, haystack: str, threshold: float = 0.75) -> bool:
    """True if `needle` appears in `haystack`, tolerant of OCR slips (e.g. rn->m).

    Real cards break exact anchors: "Surname" is read "Sumame". We slide a window
    of ~len(needle) over the line and accept a close enough match.
    """
    if not needle:
        return False
    if needle in haystack:
        return True
    if len(needle) < 4:  # too short to fuzzy-match without false positives
        return False
    for window in (len(needle) - 1, len(needle), len(needle) + 1):
        for start in range(len(haystack) - window + 1):
            candidate = haystack[start : start + window]
            if difflib.SequenceMatcher(None, needle, candidate).ratio() >= threshold:
                return True
    return False


def _find_anchor_line(lines: list[TextLine], anchor: str) -> TextLine | None:
    needle = _normalize_for_match(anchor)
    for line in lines:
        if _fuzzy_contains(needle, _normalize_for_match(line.text)):
            return line
    return None


def match_template(lines: list[TextLine], templates: list[dict]) -> dict | None:
    """Return the first template whose signature anchors are all found in `lines`."""
    for template in templates:
        required_anchors = template.get("match", {}).get("all_anchors", [])
        if required_anchors and all(
            _find_anchor_line(lines, anchor) for anchor in required_anchors
        ):
            return template
    return None


def _value_below(lines: list[TextLine], anchor_line: TextLine) -> TextLine | None:
    # Same page only: bbox coordinates are per-page, so a cross-page comparison is
    # meaningless (seen on 2-page attestations: a p1 block "below" a p0 label).
    anchor_x0, anchor_y0 = anchor_line.bbox[0], anchor_line.bbox[1]
    candidates = [
        line
        for line in lines
        if line is not anchor_line
        and line.page_index == anchor_line.page_index
        and line.bbox[1] > anchor_y0 + 5
        and abs(line.bbox[0] - anchor_x0) <= COLUMN_X_TOLERANCE
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda line: line.bbox[1] - anchor_y0)


def _value_right(lines: list[TextLine], anchor_line: TextLine) -> TextLine | None:
    anchor_x1, anchor_y0 = anchor_line.bbox[2], anchor_line.bbox[1]
    candidates = [
        line
        for line in lines
        if line is not anchor_line
        and line.page_index == anchor_line.page_index
        and line.bbox[0] >= anchor_x1 - 5
        and abs(line.bbox[1] - anchor_y0) <= ROW_Y_TOLERANCE
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda line: line.bbox[0] - anchor_x1)


def _normalize_value(value: str, rule: str) -> str:
    value = value.strip()
    if rule == "date_ddmmyyyy":
        digits = re.sub(r"\D", "", value)
        if len(digits) == 8:
            return f"{digits[4:]}-{digits[2:4]}-{digits[0:2]}"  # DDMMYYYY -> ISO
    if rule == "amount":
        # French thousands separators (space / NBSP / narrow NBSP) -> bare number.
        return re.sub(r"[\s  ]", "", value)
    if rule == "upper":
        return value.upper()
    return value


def _extract_by_pattern(document_text: str, field: dict) -> str | None:
    """Extract a field by regex over the document text (group 1, else the whole match).

    Born-digital PDFs glue a label to its value inside one PyMuPDF block, so geometry
    anchors do not apply — these fields name a regex instead.
    """
    match = re.search(field["pattern"], document_text)
    if match is None:
        return None
    value = match.group(1) if match.groups() else match.group(0)
    return _normalize_value(value, field.get("normalize", "strip"))


def extract_fields(lines: list[TextLine], template: dict) -> dict[str, str | None]:
    """Rebuild the template's named fields, by geometry anchors OR by text patterns.

    A field with a `pattern` key is extracted by regex over the document text (born-digital
    invoices, where PyMuPDF glues label+value in one block). A field with an `anchor` key
    uses the geometry path (scanned cards). A template may mix both.
    """
    document_text = "\n".join(line.text for line in lines)
    extracted: dict[str, str | None] = {}
    for field in template["fields"]:
        if "pattern" in field:
            extracted[field["name"]] = _extract_by_pattern(document_text, field)
            continue
        anchor_line = _find_anchor_line(lines, field["anchor"])
        if anchor_line is None:
            extracted[field["name"]] = None
            continue
        direction = field.get("direction", "below")
        if direction == "right":
            value_line = _value_right(lines, anchor_line)
        else:
            value_line = _value_below(lines, anchor_line)
        if value_line is None:
            extracted[field["name"]] = None
            continue
        extracted[field["name"]] = _normalize_value(
            value_line.text, field.get("normalize", "strip")
        )
    return extracted


def _parse_amount(value: str | None) -> float | None:
    """Parse a normalized amount ('1234,56' or '1234.56') to a float, or None.

    The "amount" normalize already stripped thousands separators (space / NBSP); only a
    comma-or-dot decimal remains. Returns None on an empty or unparseable value so the
    caller fails loud with a reason instead of silently passing a bad check.
    """
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


# A single validation failure, tagged by what it means for ROUTING. The kind depends on
# WHY the check failed, not on the check type: REJECT = positive proof the document is
# invalid (an incoherent value) -> auto-reject; REVIEW = the check could not conclude (a
# missing/unreadable input, an absent context) or is a soft signal (an unknown issuer, a
# pending corroboration) -> a human. "Input missing -> review, value proven wrong ->
# reject" is the guard that keeps an unread date or an unwired context from rejecting a
# legitimate document.
REVIEW = "review"
REJECT = "reject"


@dataclass(frozen=True)
class CheckFailure:
    reason: str
    kind: str  # REVIEW | REJECT
    # True when the check RAN with all its inputs and the answer is negative (a
    # DETERMINED failure); False when it could not tell (missing/unreadable input,
    # absent context — the fail-loud branches). Only determined failures may be
    # reclassified by a rule's métier `severity` override: an "I can't tell" must
    # never be hardened into a rejection (input-vs-preuve doctrine, 2026-07-03).
    determined: bool = False


def _check_sum(fields: dict[str, str | None], rule: dict) -> list[CheckFailure]:
    """Value check: the `terms` amounts must sum to the `equals` amount within tolerance.

    e.g. montant_ht + montant_tva == montant_ttc. Always REVIEW on failure: an invoice with
    an off sum is more often an OCR misread of a digit than fraud, so a human looks — it is
    not auto-rejected like a tampered certificate.
    """
    terms: list[str] = rule["terms"]
    equals_field: str = rule["equals"]
    tolerance: float = rule.get("tolerance", 0.01)

    parsed_terms = {name: _parse_amount(fields.get(name)) for name in terms}
    total = _parse_amount(fields.get(equals_field))
    missing = [name for name, amount in parsed_terms.items() if amount is None]
    if total is None:
        missing.append(equals_field)
    if missing:
        return [
            CheckFailure(
                f"sum check ({equals_field}): missing/unreadable {', '.join(missing)}",
                REVIEW,
            )
        ]

    # Compare in integer cents: amounts are 2-decimal currency, and float subtraction
    # leaves noise (100.00 + 33.33 - 133.34 != exactly -0.01) that would tip a knife-edge
    # one-cent tolerance the wrong way. Cents make the money comparison exact.
    summed = sum(amount for amount in parsed_terms.values() if amount is not None)
    difference_cents = abs(round(summed * 100) - round(total * 100))
    if difference_cents > round(tolerance * 100):
        return [
            CheckFailure(
                f"sum check failed: {' + '.join(terms)} = {summed:.2f} "
                f"!= {equals_field} = {total:.2f} (tolerance {tolerance})",
                REVIEW,
                determined=True,
            )
        ]
    return []


def _parse_iso_date(value: str | None) -> date | None:
    """Parse an ISO `YYYY-MM-DD` value (what `normalize: date_ddmmyyyy` produces) to a
    date, or None when empty/unreadable so the caller fails loud with a reason."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _add_years(start: date, years: int) -> date:
    """`start` plus a whole number of calendar years, folding Feb 29 to Feb 28 when the
    target year is not a leap year (the only day that has no exact anniversary)."""
    try:
        return start.replace(year=start.year + years)
    except ValueError:
        return start.replace(year=start.year + years, day=28)


def _missing_dates(named_dates: dict[str, date | None]) -> list[str]:
    return [name for name, value in named_dates.items() if value is None]


def _check_date_order(
    fields: dict[str, str | None], rule: dict, today: date | None
) -> list[CheckFailure]:
    """Value check: `earlier` must fall strictly before `later`, and (opt-in
    `require_future`) `later` must not already be in the past. A swapped/incoherent window
    or an expired certificate is proven-invalid (REJECT); an unreadable date is REVIEW."""
    earlier_name: str = rule["earlier"]
    later_name: str = rule["later"]
    earlier = _parse_iso_date(fields.get(earlier_name))
    later = _parse_iso_date(fields.get(later_name))
    missing = _missing_dates({earlier_name: earlier, later_name: later})
    if missing:
        return [
            CheckFailure(
                f"date_order check: missing/unreadable {', '.join(missing)}", REVIEW
            )
        ]
    assert earlier is not None and later is not None
    failures: list[CheckFailure] = []
    if not earlier < later:
        failures.append(
            CheckFailure(
                f"date_order check failed: {earlier_name} {earlier.isoformat()} is not "
                f"before {later_name} {later.isoformat()}",
                REJECT,
                determined=True,
            )
        )
    if rule.get("require_future"):
        reference_date = today or date.today()
        if later < reference_date:
            failures.append(
                CheckFailure(
                    f"date_order check failed: {later_name} {later.isoformat()} is in "
                    f"the past (expired as of {reference_date.isoformat()})",
                    REJECT,
                    determined=True,
                )
            )
    return failures


def _check_date_span(fields: dict[str, str | None], rule: dict) -> list[CheckFailure]:
    """Value check: `end` must equal `start` plus `years` calendar years, within
    `tolerance_days`. A regulatory validity has a fixed duration (electrical habilitation
    ~3 years), so a date lengthened by pen breaks the equation (REJECT); an unreadable date
    is REVIEW."""
    start_name: str = rule["start"]
    end_name: str = rule["end"]
    years: int = rule["years"]
    tolerance_days: int = rule.get("tolerance_days", 2)
    start = _parse_iso_date(fields.get(start_name))
    end = _parse_iso_date(fields.get(end_name))
    missing = _missing_dates({start_name: start, end_name: end})
    if missing:
        return [
            CheckFailure(
                f"date_span check: missing/unreadable {', '.join(missing)}", REVIEW
            )
        ]
    assert start is not None and end is not None
    expected_end = _add_years(start, years)
    difference_days = abs((end - expected_end).days)
    if difference_days > tolerance_days:
        return [
            CheckFailure(
                f"date_span check failed: {end_name} {end.isoformat()} != {start_name} "
                f"+ {years}y ({expected_end.isoformat()}), off by {difference_days} days "
                f"(tolerance {tolerance_days})",
                REJECT,
                determined=True,
            )
        ]
    return []


def _check_vocabulary(fields: dict[str, str | None], rule: dict) -> list[CheckFailure]:
    """Value check: every token of `field` must belong to the closed `allowed` list
    (case-insensitive). An invented code is proven-invalid (REJECT); a missing/unreadable
    field is REVIEW."""
    field_name: str = rule["field"]
    allowed: list[str] = rule["allowed"]
    value = fields.get(field_name)
    if not value:
        return [
            CheckFailure(f"vocabulary check ({field_name}): missing/unreadable", REVIEW)
        ]
    allowed_folded = {entry.strip().casefold() for entry in allowed}
    tokens = [token for token in re.split(r"[\s,;/]+", value) if token]
    unknown = [token for token in tokens if token.casefold() not in allowed_folded]
    if unknown:
        return [
            CheckFailure(
                f"vocabulary check failed ({field_name}): "
                f"{', '.join(unknown)} not in the allowed list",
                REJECT,
                determined=True,
            )
        ]
    return []


@dataclass
class AttestationReference:
    """A validated `attestation_formation` on file (D1), as `corroborated_by` needs to see
    it: the holder's name and the training's validity window (ISO dates)."""

    holder_name: str
    issue_date: str
    expiry_date: str


@dataclass
class ValidationContext:
    """External state the context-dependent anti-fraud checks read. Every field is optional;
    a contextual check declared WITHOUT the state it needs FAILS LOUD (-> needs_review),
    never a silent pass — an absent registry cannot prove an issuer legitimate.

    - `ci_reference_name`: the holder name on the technician's CI record (reconcile_ci).
    - `issuer_registry`: the recognized organisms' identifiers, SIRET preferred over a
      copyable name (issuer_registry); compared after strict normalization.
    - `validated_attestations`: the validated attestations on file (corroborated_by)."""

    ci_reference_name: str | None = None
    issuer_registry: frozenset[str] | None = None
    validated_attestations: list[AttestationReference] | None = None


def _check_reconcile_ci(
    fields: dict[str, str | None], rule: dict, context: ValidationContext | None
) -> list[CheckFailure]:
    """Context check: the holder name must STRICTLY match the CI record on file (accent
    folding only — Ahmed != Hamed). A genuine mismatch is the sibling fraud -> REJECT; an
    absent CI reference or unreadable holder is "can't tell" -> REVIEW (a proven mismatch
    rejects, but a missing reference must never reject a legitimate document)."""
    field_name: str = rule["field"]
    if context is None or context.ci_reference_name is None:
        return [
            CheckFailure(
                f"reconcile_ci ({field_name}): no CI reference on file to compare "
                "against",
                REVIEW,
            )
        ]
    value = fields.get(field_name)
    if not value:
        return [
            CheckFailure(
                f"reconcile_ci ({field_name}): holder name missing/unreadable", REVIEW
            )
        ]
    if _strict_identity_key(value) != _strict_identity_key(context.ci_reference_name):
        return [
            CheckFailure(
                f"reconcile_ci check failed ({field_name}): holder {value!r} does not "
                f"match the CI record {context.ci_reference_name!r} (strict)",
                REJECT,
                determined=True,
            )
        ]
    return []


def _check_issuer_registry(
    fields: dict[str, str | None], rule: dict, context: ValidationContext | None
) -> list[CheckFailure]:
    """Context check: the issuer identifier read must belong to the curated registry of
    recognized organisms (strict normalization). Always REVIEW on failure: an unknown
    issuer may be a NEW legitimate organism a human adds to the registry, not a proven
    forgery — so it routes to a human, it is not auto-rejected."""
    field_name: str = rule["field"]
    if context is None or context.issuer_registry is None:
        return [
            CheckFailure(
                f"issuer_registry ({field_name}): no organism registry available",
                REVIEW,
            )
        ]
    value = fields.get(field_name)
    if not value:
        return [
            CheckFailure(
                f"issuer_registry ({field_name}): issuer missing/unreadable", REVIEW
            )
        ]
    recognized = {_strict_identity_key(entry) for entry in context.issuer_registry}
    if _strict_identity_key(value) not in recognized:
        return [
            CheckFailure(
                f"issuer_registry check failed ({field_name}): issuer {value!r} is not "
                "in the recognized-organism registry",
                REVIEW,
                determined=True,
            )
        ]
    return []


def _check_corroborated_by(
    fields: dict[str, str | None], rule: dict, context: ValidationContext | None
) -> list[CheckFailure]:
    """Context check: a self-declared `titre_habilitation` is AUTO only if a validated
    `attestation_formation` on file corroborates it — same holder (strict) and coherent
    dates (the titre issued WITHIN the training's validity window). Always REVIEW on
    failure: an uncorroborated titre is PENDING an attestation, not proven false — "ma mère
    peut me faire une certif" means it never auto-validates, but a human decides."""
    holder_field: str = rule["holder_field"]
    issue_field: str = rule["issue_field"]
    if context is None or context.validated_attestations is None:
        return [
            CheckFailure(
                f"corroborated_by ({holder_field}): no validated attestations on file",
                REVIEW,
            )
        ]
    holder = fields.get(holder_field)
    titre_issue = _parse_iso_date(fields.get(issue_field))
    if not holder:
        return [
            CheckFailure(
                f"corroborated_by ({holder_field}): holder name missing/unreadable",
                REVIEW,
            )
        ]
    if titre_issue is None:
        return [
            CheckFailure(
                f"corroborated_by ({issue_field}): titre issue date missing/unreadable",
                REVIEW,
            )
        ]
    holder_key = _strict_identity_key(holder)
    for attestation in context.validated_attestations:
        if _strict_identity_key(attestation.holder_name) != holder_key:
            continue
        attestation_issue = _parse_iso_date(attestation.issue_date)
        attestation_expiry = _parse_iso_date(attestation.expiry_date)
        if attestation_issue is None or attestation_expiry is None:
            continue
        if attestation_issue <= titre_issue <= attestation_expiry:
            return []  # a coherent, validated attestation corroborates the titre
    return [
        CheckFailure(
            f"corroborated_by check failed ({holder_field}): no validated attestation "
            f"corroborates the self-declared titre for {holder!r} at "
            f"{titre_issue.isoformat()}",
            REVIEW,
            determined=True,
        )
    ]


@dataclass
class ValidationOutcome:
    """A template's validation, CLASSIFIED by what each failure means for routing.

    `reject_reasons` = positive proof of invalidity -> the document is auto-rejected
    (terminal, no human). `review_reasons` = an unknown/pending/undetermined signal a human
    handles. `verdict` picks the strongest present: reject beats review beats auto (a
    proven-invalid document is not softened to "please review" just because it also carries
    a pending or undetermined check)."""

    reject_reasons: list[str]
    review_reasons: list[str]

    @property
    def verdict(self) -> str:
        if self.reject_reasons:
            return "reject"
        if self.review_reasons:
            return "review"
        return "auto"


def _evaluate_rule(
    fields: dict[str, str | None],
    rule: dict,
    today: date | None,
    context: ValidationContext | None,
) -> list[CheckFailure]:
    """Run one validation rule -> its classified failures ([] when it passes)."""
    check = rule.get("check")
    if check == "present":
        field_name = rule["field"]
        if not fields.get(field_name):
            # A required field not read is a coverage problem, not proof of a forgery.
            return [
                CheckFailure(f"{field_name}: required (presence) but not read", REVIEW)
            ]
        return []
    if check == "sum":
        return _check_sum(fields, rule)
    if check == "date_order":
        return _check_date_order(fields, rule, today)
    if check == "date_span":
        return _check_date_span(fields, rule)
    if check == "vocabulary":
        return _check_vocabulary(fields, rule)
    if check == "reconcile_ci":
        return _check_reconcile_ci(fields, rule, context)
    if check == "issuer_registry":
        return _check_issuer_registry(fields, rule, context)
    if check == "corroborated_by":
        return _check_corroborated_by(fields, rule, context)
    return [CheckFailure(f"unknown validation check: {check!r}", REVIEW)]


def evaluate_validation(
    fields: dict[str, str | None],
    validation: dict,
    *,
    today: date | None = None,
    context: ValidationContext | None = None,
) -> ValidationOutcome:
    """Evaluate a template's `validation` block and CLASSIFY the failures for routing.

    Config-driven: the checks travel WITH the template (the Backoffice curates them), so the
    SAME evaluator serves every document type. All checks are COMPUTED; the template's
    `required` block says which are REQUIRED (compute-all/config-requires). Each failing
    check tags its failures REJECT (positive proof of invalidity) or REVIEW (undetermined /
    pending / soft signal) — the tag depends on the failure BRANCH, not the check type: a
    proven-wrong date rejects, an unreadable one is only REVIEW. Check kinds, per template:
      - present:        the named field must be non-empty (presence, value-agnostic).
      - sum:            `terms` must sum to `equals` within `tolerance`.
      - date_order:     `earlier` before `later` (+ opt-in `require_future` not-expired).
      - date_span:      `end` == `start` + `years` calendar years, within `tolerance_days`.
      - vocabulary:     every token of `field` belongs to the closed `allowed` list.
      - reconcile_ci:   holder `field` strictly matches the CI record (context).
      - issuer_registry:issuer `field` is in the recognized-organism registry (context).
      - corroborated_by:a self-declared titre is backed by a validated attestation (context).
    `today` overrides the clock date_order's freshness side compares against; `context`
    carries the external state the last three checks read (both injected for reproducible
    tests). A context check declared without its context resolves to REVIEW (undetermined),
    never a silent pass and never a reject.

    SEVERITY OVERRIDE (métier knob, 2026-07-12): a rule may carry `"severity": "reject" |
    "review"` to harden or soften its DETERMINED failures — e.g. once the organism
    registry is trusted, `issuer_registry` hardens from review to non conforme (« émetteur
    ≠ Y → non valide »). It never touches an UNDETERMINED failure: "I can't tell" (missing
    input, absent registry) stays review whatever the config says — the fail-loud guard is
    not overridable. An unknown severity value is itself surfaced as a review reason
    (a config typo must never pass silently).
    """
    reject_reasons: list[str] = []
    review_reasons: list[str] = []
    for rule in validation.get("required", []):
        severity: str | None = rule.get("severity")
        if severity is not None and severity not in (REJECT, REVIEW):
            review_reasons.append(
                f"unknown severity {severity!r} on check '{rule.get('check')}' — "
                "config error (expected 'reject' or 'review'), routed to review"
            )
            severity = None
        for failure in _evaluate_rule(fields, rule, today, context):
            kind = failure.kind
            if severity is not None and failure.determined:
                kind = severity
            if kind == REJECT:
                reject_reasons.append(failure.reason)
            else:
                review_reasons.append(failure.reason)
    return ValidationOutcome(reject_reasons, review_reasons)


def validate_fields(
    fields: dict[str, str | None],
    validation: dict,
    *,
    today: date | None = None,
    context: ValidationContext | None = None,
) -> list[str]:
    """Flat failure reasons for a template's `validation` block ([] = green -> auto).

    Backward-compatible thin wrapper over `evaluate_validation` for callers that only care
    whether validation passed (the drafting / naming re-test gates, the suggestion fit
    gate). The routing layer calls `evaluate_validation` instead, to tell a proven-invalid
    document (reject) apart from an unknown one (review)."""
    outcome = evaluate_validation(fields, validation, today=today, context=context)
    return outcome.reject_reasons + outcome.review_reasons
