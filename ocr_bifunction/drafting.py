"""Stages D-a/D-b — the template DRAFTING lane, deterministic half.

The growth loop can read, categorize (closed list), extract, validate, review and
promote — but a template is still born from a developer writing JSON. This module is
the deterministic core of the lane that removes that step:

- D-a `cluster_unknown_documents`: group UNKNOWN documents by whole-document TF-IDF
  cosine similarity (the same lexical core as the RAG lane). A layout that RETURNS
  forms a cluster and deserves a template; a one-off stays a singleton (RAG material).
- D-b `draft_from_cluster`: cross-document INVARIANCE inside one cluster. Text found
  in EVERY document is structural (type vocabulary, field labels, the issuing body
  read as text) and becomes anchor material; zones that vary next to an invariant
  label become field candidates. Invariance IS the PII filter: a line identical
  across documents belonging to different people cannot be personal data.
- The draft is RE-TESTED (the generalized gate 2) before it leaves this module: it
  must match, and extract a VARYING value, on every document of its own cluster —
  unstable fields are dropped, and a draft with nothing left is rejected.

The SLM half (naming fields, proposing normalize/pattern rules and validation checks)
only ever DECORATES a deterministic draft; it never creates one. And no draft is
activated without a human decision (review page -> promotion D2).

Known limit (v1): invariance works at TextLine granularity. A label glued to its
value inside one line ("Delivree le : 12/03/2024") yields neither an invariant line
nor a geometry field; a prefix-pattern field family can cover that shape when a real
cluster demands it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ocr_bifunction.rag import Chunk, TfidfRetriever
from ocr_bifunction.reader import TextLine

# Drafting must use the SAME primitives matching/extraction use: an anchor is only
# "invariant" if match-time fuzzy search will find it, and a value zone is only a
# field candidate if extract-time geometry will pick it. These helpers are private by
# convention but shared inside the package on purpose — duplicating them would let the
# two semantics drift apart.
from ocr_bifunction.template import (
    _find_anchor_line,
    _normalize_for_match,
    _value_below,
    _value_right,
    extract_fields,
    match_template,
    validate_fields,
)

# Same-layout documents share their structural text (labels, vocabulary, issuing
# body) and differ only in values, so their whole-document cosine sits high; unrelated
# documents sit low. 0.5 splits the two regimes with margin on the corpora seen so far.
DEFAULT_SIMILARITY_THRESHOLD = 0.5
# Below this many normalized characters, the fuzzy anchor search refuses to fuzzy-match
# and short strings collide across unrelated lines anyway.
_MINIMUM_ANCHOR_CHARACTERS = 4
_MAXIMUM_MATCH_ANCHORS = 3
# Very long invariants (legal boilerplate blocks) re-segment differently under OCR, so
# match anchors prefer short-to-medium lines; longer ones are kept only as a fallback.
_PREFERRED_ANCHOR_MAXIMUM_LENGTH = 60
_PREFERRED_ANCHOR_MINIMUM_LENGTH = 8
# Anchor material must contain at least one real word: a bare date or number that
# happens to repeat across documents is not personal data (two people share it), but it
# is a brittle anchor — the next legitimate document breaks it.
_ALPHABETIC_RUN_PATTERN = re.compile(r"[a-zA-ZÀ-ÿ]{4,}")


@dataclass
class DraftingDocument:
    """One unknown document as the drafting lane sees it: the source name (display
    only — it may contain a person's name, so it never enters a draft), the flat text
    (clustering material) and the geometry lines (invariance material)."""

    source: str
    text: str
    lines: list[TextLine]


@dataclass
class DraftReport:
    """Outcome of drafting one cluster: the re-tested draft (or None with reasons),
    what the gate dropped, and the per-document extractions so a human can SEE the
    variance the draft captured."""

    template: dict | None
    anchors: list[str] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    extractions_by_source: dict[str, dict[str, str | None]] = field(
        default_factory=dict
    )


def pairwise_similarity(documents: list[DraftingDocument]) -> list[list[float]]:
    """Whole-document TF-IDF cosine matrix, reusing the RAG retriever as-is (each
    document indexed as one chunk; querying with a document's own text vectorizes it
    identically, so the scores are the symmetric pairwise cosines)."""
    retriever = TfidfRetriever()
    retriever.index(
        [
            Chunk(text=document.text, source=document.source, index=position)
            for position, document in enumerate(documents)
        ]
    )
    matrix = [[0.0] * len(documents) for _ in documents]
    for position, document in enumerate(documents):
        for chunk, score in retriever.query(document.text, top_k=len(documents)):
            matrix[position][chunk.index] = score
    return matrix


def cluster_unknown_documents(
    documents: list[DraftingDocument],
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[list[DraftingDocument]]:
    """D-a — group unknown documents by layout, via whole-document lexical similarity.

    Greedy single-link: each document joins the first cluster holding at least one
    member within the threshold, else starts its own — deterministic in input order.
    A cluster of size 1 is a one-off and stays RAG material; only a layout that
    RETURNS is worth a template.
    """
    if not documents:
        return []
    matrix = pairwise_similarity(documents)
    clusters: list[list[int]] = []
    for position in range(len(documents)):
        joined = False
        for members in clusters:
            if any(
                matrix[position][member] >= similarity_threshold for member in members
            ):
                members.append(position)
                joined = True
                break
        if not joined:
            clusters.append([position])
    return [[documents[member] for member in members] for members in clusters]


def _is_structural_text(raw_text: str) -> bool:
    return bool(_ALPHABETIC_RUN_PATTERN.search(raw_text))


def _sanitize_anchor_text(raw_text: str) -> str:
    # A born-digital block can span several physical lines; collapse the whitespace so
    # the stored anchor is a clean single line (match-normalization is unaffected).
    return " ".join(raw_text.split())


def find_invariant_lines(cluster: list[DraftingDocument]) -> list[TextLine]:
    """Lines of the FIRST document found (same fuzzy predicate as matching) in EVERY
    other document of the cluster. Cross-document invariance is the mechanical PII
    filter: text identical across documents belonging to different people cannot be
    personal data. Deduplicated on normalized form; word-free lines (bare dates,
    numbers) are excluded as brittle anchor material.
    """
    base_document = cluster[0]
    other_documents = cluster[1:]
    invariants: list[TextLine] = []
    seen_normalized: set[str] = set()
    for line in base_document.lines:
        normalized = _normalize_for_match(line.text)
        if len(normalized) < _MINIMUM_ANCHOR_CHARACTERS:
            continue
        if normalized in seen_normalized or not _is_structural_text(line.text):
            continue
        if all(
            _find_anchor_line(other.lines, line.text) is not None
            for other in other_documents
        ):
            invariants.append(line)
            seen_normalized.add(normalized)
    return invariants


def _select_match_anchors(invariant_lines: list[TextLine]) -> list[str]:
    sanitized = [_sanitize_anchor_text(line.text) for line in invariant_lines]
    preferred = [
        text
        for text in sanitized
        if _PREFERRED_ANCHOR_MINIMUM_LENGTH
        <= len(text)
        <= _PREFERRED_ANCHOR_MAXIMUM_LENGTH
    ]
    fallback = [text for text in sanitized if text not in preferred]
    ranked = sorted(preferred, key=len, reverse=True) + sorted(
        fallback, key=len, reverse=True
    )
    return ranked[:_MAXIMUM_MATCH_ANCHORS]


def _placeholder_field_name(label_text: str, taken_names: set[str]) -> str:
    """Deterministic placeholder derived from the label — the SLM proposes real names
    later, the human decides. ASCII-folded so the name is a safe identifier."""
    folded = (
        unicodedata.normalize("NFKD", label_text)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "_", folded.lower()).strip("_")[:40] or "champ"
    name = slug
    suffix = 2
    while name in taken_names:
        name = f"{slug}_{suffix}"
        suffix += 1
    taken_names.add(name)
    return name


def _seed_field_candidates(
    cluster: list[DraftingDocument], invariant_lines: list[TextLine]
) -> list[dict]:
    """One field candidate per invariant label that has a VARIABLE zone next to it in
    the base document ("below" preferred, "right" fallback). The candidates are only
    seeds: the re-test gate keeps the ones that extract a varying value on EVERY
    document of the cluster."""
    base_document = cluster[0]
    invariant_normalized = {_normalize_for_match(line.text) for line in invariant_lines}
    candidates: list[dict] = []
    taken_names: set[str] = set()
    for label_line in invariant_lines:
        for direction, value_finder in (
            ("below", _value_below),
            ("right", _value_right),
        ):
            value_line = value_finder(base_document.lines, label_line)
            if value_line is None:
                continue
            if _normalize_for_match(value_line.text) in invariant_normalized:
                continue  # the neighbor is structural too (e.g. the next label)
            anchor_text = _sanitize_anchor_text(label_line.text)
            candidates.append(
                {
                    "name": _placeholder_field_name(anchor_text, taken_names),
                    "anchor": anchor_text,
                    "direction": direction,
                }
            )
            break
    return candidates


def _assemble_draft(
    template_id: str,
    category: str,
    cluster_size: int,
    match_anchors: list[str],
    fields: list[dict],
) -> dict:
    return {
        "template_id": template_id,
        "category": category,
        "description": (
            f"DRAFT auto-extracted from a cluster of {cluster_size} unknown "
            "documents. Anchors are cross-document invariants (mechanical PII "
            "filter); field names are deterministic placeholders pending SLM "
            "naming and human curation."
        ),
        "match": {"all_anchors": match_anchors},
        "fields": fields,
        "validation": {
            "comment": (
                "Draft candidates only: every stable field is proposed as a "
                "presence check; the reviewer decides which checks become "
                "REQUIRED at validation time (compute-all/config-requires)."
            ),
            "required": [
                {"field": field_entry["name"], "check": "present"}
                for field_entry in fields
            ],
        },
    }


def draft_from_cluster(
    cluster: list[DraftingDocument], category: str, template_id: str
) -> DraftReport:
    """D-b — draft a template from one cluster and RE-TEST it on the whole cluster.

    The gate (generalized gate 2): the draft must MATCH on every document; a field is
    kept only if it extracts a non-empty value on every document AND the values are
    not all identical (a constant is structure, not a field). A draft with no anchors
    or no surviving field is rejected with reasons.
    """
    if len(cluster) < 2:
        return DraftReport(
            template=None,
            reasons=["cluster of 1: invariance is undefined, the one-off stays RAG"],
        )

    invariant_lines = find_invariant_lines(cluster)
    if not invariant_lines:
        return DraftReport(
            template=None, reasons=["no cross-document invariant line found"]
        )
    match_anchors = _select_match_anchors(invariant_lines)
    candidates = _seed_field_candidates(cluster, invariant_lines)
    draft = _assemble_draft(
        template_id, category, len(cluster), match_anchors, candidates
    )

    # Gate step 1 — the draft must MATCH every document of its own cluster.
    reasons: list[str] = []
    for document in cluster:
        if match_template(document.lines, [draft]) is None:
            reasons.append(f"{document.source}: draft anchors not all found")
    if reasons:
        return DraftReport(template=None, anchors=match_anchors, reasons=reasons)

    # Gate step 2 — keep only fields that extract a VARYING value everywhere.
    extractions_by_source = {
        document.source: extract_fields(document.lines, draft) for document in cluster
    }
    kept_fields: list[dict] = []
    dropped_fields: list[str] = []
    for field_entry in candidates:
        field_name = field_entry["name"]
        values = [
            extractions_by_source[document.source].get(field_name)
            for document in cluster
        ]
        if any(not value for value in values):
            dropped_fields.append(f"{field_name} (not extracted on every document)")
            continue
        if len(set(values)) == 1:
            dropped_fields.append(f"{field_name} (constant across the cluster)")
            continue
        kept_fields.append(field_entry)
    if not kept_fields:
        return DraftReport(
            template=None,
            anchors=match_anchors,
            dropped_fields=dropped_fields,
            reasons=["no stable variable field across the cluster"],
        )

    final_draft = _assemble_draft(
        template_id, category, len(cluster), match_anchors, kept_fields
    )
    kept_names = {field_entry["name"] for field_entry in kept_fields}
    final_extractions = {
        source: {
            name: value for name, value in extraction.items() if name in kept_names
        }
        for source, extraction in extractions_by_source.items()
    }

    # Gate step 3 — the final draft's own validation must be green on the cluster
    # (presence of every kept field; by construction, so a failure here is a bug).
    for document in cluster:
        failures = validate_fields(
            final_extractions[document.source], final_draft["validation"]
        )
        reasons.extend(f"{document.source}: {failure}" for failure in failures)
    if reasons:
        return DraftReport(
            template=None,
            anchors=match_anchors,
            dropped_fields=dropped_fields,
            reasons=reasons,
        )

    return DraftReport(
        template=final_draft,
        anchors=match_anchors,
        dropped_fields=dropped_fields,
        extractions_by_source=final_extractions,
    )
