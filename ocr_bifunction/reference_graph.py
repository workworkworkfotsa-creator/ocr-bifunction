"""Reference graph over contract article chunks — the Étape 2 machinery.

Étape 1 (retrieval) FINDS the clause that modifies something; it does not RESOLVE the link
it describes ("Avenant Art.2 —REMPLACE→ Contrat Annexe 4/5"). This module builds that link
explicitly: for each article chunk, one LLM call extracts its outgoing reference edges
(`generation.extract_references`), and each edge's `ancien`/`nouveau` string is resolved to
an existing node (another chunk) when one matches — otherwise the target is DANGLING.

A dangling target is a FEATURE, not a failure: "Annexe 4 du Contrat" is cited but not present
as a segmented node (known segmentation debt — annexes are absorbed by the last article), so
a dangling edge is a completeness signal ("this doc references something the corpus lacks").

Query-time is a 1-hop traversal: retrieve the relevant node, then follow its outgoing edges
to show the source clause + its targets (resolved or dangling). No graph database — the graph
is a list of edges over the in-memory chunks, brute-forced like the rest of the POC.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field

from ocr_bifunction.generation import Generator, Reference, extract_references
from ocr_bifunction.rag import Chunk

# A reference string names a "type + id" element: "Annexe 4 du Contrat", "Article 2",
# "Avenant n°7". Capture the leading (type, id) to build a resolution key; the optional
# "n°"/"no" between type and id is skipped. Roman OR arabic id (both schemes occur).
_REFERENCE_KEY_PATTERN = re.compile(
    r"(article|annexe|avenant)\s+(?:n[°ºo]\s*)?([ivxlc]+|\d+)", re.IGNORECASE
)

# A genuine reference NAMES a document element: it LEADS with a document-element type
# (article / annexe / avenant / contrat), possibly after a determiner ("l'", "les", "du"…).
# The LLM over-extracts on prose — emitting edges whose `ancien` is a delay, a date, a value,
# or a party ("délai approprié", "01/04/2023 pour une durée d'un an", "les Parties"). Those do
# NOT lead with a document element, so anchoring the check at the HEAD (not a substring, which
# would wrongly keep "…avant le terme du Contrat Cadre") drops them. This is a structural
# validity check on the model's output, not a rule-based re-extraction: the definition of "a
# reference names a document" is stable, unlike the drifting business categories LLM extraction
# was chosen to avoid.
_LEADING_DETERMINER_PATTERN = re.compile(
    r"^(?:l['’]|d['’]|les?\s+|du\s+|des\s+|de\s+la\s+|au[x]?\s+|à\s+l['’])",
    re.IGNORECASE,
)
_DOCUMENT_ELEMENT_HEAD_PATTERN = re.compile(
    r"^(?:articles?|annexes?|avenants?|contrat)\b", re.IGNORECASE
)


def is_document_reference(text: str) -> bool:
    """True when `text` NAMES a document element (leads with article/annexe/avenant/contrat,
    after an optional determiner) — i.e. it is a genuine reference, not over-extracted prose."""
    stripped = _LEADING_DETERMINER_PATTERN.sub("", text.strip(), count=1).strip()
    return bool(_DOCUMENT_ELEMENT_HEAD_PATTERN.match(stripped))


@dataclass
class ReferenceEdge:
    """One resolved edge: the article that created it, plus each endpoint resolved to a node
    (Chunk) or left None (dangling — cited but absent from the corpus)."""

    reference: Reference
    source: Chunk
    ancien_target: Chunk | None
    nouveau_target: Chunk | None

    @property
    def ancien_dangling(self) -> bool:
        return self.ancien_target is None

    @property
    def nouveau_dangling(self) -> bool:
        # A None `nouveau` string (e.g. RENVOIE) is "no endpoint", not a dangling one.
        return self.reference.nouveau is not None and self.nouveau_target is None


@dataclass
class ReferenceGraph:
    """The article chunks (nodes) and the reference edges between/out of them."""

    chunks: list[Chunk] = field(default_factory=list)
    edges: list[ReferenceEdge] = field(default_factory=list)

    def outgoing(self, chunk: Chunk) -> list[ReferenceEdge]:
        """The edges created BY `chunk` (identity match — same object the retriever ranks)."""
        return [edge for edge in self.edges if edge.source is chunk]


def _fold(text: str) -> str:
    """Lowercase + strip accents + collapse whitespace — for tolerant string matching."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", stripped).strip().lower()


def _reference_key(reference_text: str) -> str | None:
    """The 'type id' key of a reference string, e.g. 'annexe 4' — or None if none is named."""
    match = _REFERENCE_KEY_PATTERN.search(reference_text)
    if not match:
        return None
    return f"{match.group(1).lower()} {match.group(2).lower()}"


def _resolve(reference_text: str | None, chunks: list[Chunk]) -> Chunk | None:
    """Resolve a reference string to the chunk whose heading names the same element.

    Matches on the 'type id' key (e.g. 'article 2') folded into the chunk heading. Annexes
    are not segmented as their own nodes, so an 'annexe N' reference resolves to nothing —
    a dangling edge, by design. Returns the first matching node, or None."""
    if not reference_text:
        return None
    key = _reference_key(reference_text)
    if not key:
        return None
    for chunk in chunks:
        if chunk.heading and key in _fold(chunk.heading):
            return chunk
    return None


def build_reference_graph(
    chunks: list[Chunk],
    generator: Generator,
    on_progress: Callable[[Chunk, list[Reference], list[Reference]], None]
    | None = None,
) -> ReferenceGraph:
    """Extract reference edges from every article chunk and resolve their endpoints.

    One LLM call per chunk (cost is ~N calls — batch/nightly territory). Edges whose `ancien`
    is not a document reference (`is_document_reference`) are DROPPED before the graph is
    built — that is the denoising of the model's over-extraction on prose. `on_progress` is
    called after each chunk with (chunk, kept_references, dropped_references), so a runner can
    stream progress AND observe what the filter removed."""
    edges: list[ReferenceEdge] = []
    for chunk in chunks:
        kept: list[Reference] = []
        dropped: list[Reference] = []
        for reference in extract_references(generator, chunk.text):
            (kept if is_document_reference(reference.ancien) else dropped).append(
                reference
            )
        for reference in kept:
            edges.append(
                ReferenceEdge(
                    reference=reference,
                    source=chunk,
                    ancien_target=_resolve(reference.ancien, chunks),
                    nouveau_target=_resolve(reference.nouveau, chunks),
                )
            )
        if on_progress is not None:
            on_progress(chunk, kept, dropped)
    return ReferenceGraph(chunks=chunks, edges=edges)
