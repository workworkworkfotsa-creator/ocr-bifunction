"""Lane RAG — the retrieval lane for NON-structured documents (memos, articles).

The 2-lane routing (cf. CLAUDE.md) sends a document that matches NO structured template
here: there is nothing to extract, so instead of fields we give the human a handle on an
unidentified doc — a CONTENT SUMMARY (what is this about?) and a searchable INDEX (top-k
passages for a query). No auto-validation: an unidentified doc is, by construction, for the
human; this lane just makes it legible and findable.

The retrieval engine is a JETTISONABLE SLOT, exactly like `OcrEngine`: the `Retriever`
Protocol is the seam, and the first impl is a self-contained lexical TF-IDF (zero heavy
deps, no model download) — enough to prove the whole lane shape (chunk -> index -> rank)
on real docs. A semantic embedding retriever (GGUF/sentence-transformers) swaps in behind
the same interface later, when the lexical baseline is the thing being out-grown.

The SAME TF-IDF core feeds both products: cosine over chunk vectors for retrieval, and the
top-weighted terms + sentences for the extractive summary. No LLM, no download.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Keep accented Latin letters and digits; everything else splits tokens.
_TOKEN_PATTERN = re.compile(r"[a-zA-ZÀ-ÿ0-9]+")
# Split on sentence-final punctuation followed by whitespace (good enough for extraction).
_SENTENCE_PATTERN = re.compile(r"(?<=[.!?])\s+")
_MINIMUM_TOKEN_LENGTH = 3

# A compact FR + EN stopword set — denoises TF-IDF without a linguistics dependency.
STOPWORDS: frozenset[str] = frozenset(
    """
    le la les un une des du de da au aux et ou ni mais donc or car que qui quoi dont
    ce cet cette ces son sa ses leur leurs mon ma mes ton ta tes notre nos votre vos
    pour par sur sous dans avec sans vers chez entre est sont ete etre avoir ont avait
    plus moins tres bien aussi comme tout tous toute toutes ne pas plus rien on nous vous
    ils elles il elle je tu se sa son lui eux y en il-y-a cela ceci celui celle
    the a an of to and or in on at for with without from by as is are was were be been
    this that these those it its his her their our your my we you they he she them us
    not no any all can will would should could may might one two
    """.split()
)


@dataclass
class Chunk:
    """One indexed passage: its text plus where it came from (source + ordinal)."""

    text: str
    source: str
    index: int


@runtime_checkable
class Retriever(Protocol):
    """The jettisonable retrieval slot. Any engine that indexes chunks and ranks them
    against a query fits — lexical TF-IDF today, semantic embeddings tomorrow."""

    name: str

    def index(self, chunks: list[Chunk]) -> None:
        """Build whatever internal representation the query step needs."""
        ...

    def query(self, text: str, top_k: int = 3) -> list[tuple[Chunk, float]]:
        """Return the top_k (chunk, score) pairs most relevant to `text`, best first."""
        ...


def tokenize(text: str) -> list[str]:
    """Lowercased content tokens: accents kept, short tokens and stopwords dropped."""
    return [
        token
        for raw in _TOKEN_PATTERN.findall(text)
        for token in (raw.lower(),)
        if len(token) >= _MINIMUM_TOKEN_LENGTH and token not in STOPWORDS
    ]


def split_sentences(text: str) -> list[str]:
    """Split into sentences on terminal punctuation; drop blanks and tiny fragments."""
    sentences = [sentence.strip() for sentence in _SENTENCE_PATTERN.split(text)]
    return [sentence for sentence in sentences if len(sentence) > 1]


def chunk_document(text: str, source: str, target_tokens: int = 120) -> list[Chunk]:
    """Pack the document's paragraphs into chunks of about `target_tokens` content tokens.

    Paragraphs (newline-separated) are the natural unit; consecutive ones are greedily
    merged until a chunk reaches the target size, so a chunk is a coherent passage rather
    than an arbitrary token window. Returns at least one chunk for non-empty text.
    """
    paragraphs = [block.strip() for block in text.split("\n") if block.strip()]
    chunks: list[Chunk] = []
    current_parts: list[str] = []
    current_token_count = 0
    for paragraph in paragraphs:
        current_parts.append(paragraph)
        current_token_count += len(tokenize(paragraph))
        if current_token_count >= target_tokens:
            chunks.append(Chunk("\n".join(current_parts), source, len(chunks)))
            current_parts = []
            current_token_count = 0
    if current_parts:
        chunks.append(Chunk("\n".join(current_parts), source, len(chunks)))
    return chunks


def _l2_normalize(vector: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(weight * weight for weight in vector.values()))
    if norm == 0.0:
        return vector
    return {term: weight / norm for term, weight in vector.items()}


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    # Both vectors are L2-normalized, so the dot product over shared terms IS the cosine.
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    return sum(weight * larger.get(term, 0.0) for term, weight in smaller.items())


class TfidfRetriever:
    """Self-contained lexical TF-IDF retriever — no model, no download, no heavy deps.

    Smoothed IDF over the indexed chunks, raw term frequency, L2-normalized vectors so a
    query ranks chunks by cosine similarity. The proven baseline behind the `Retriever`
    slot; a semantic engine replaces it without touching the lane around it.
    """

    name = "tfidf"

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._inverse_document_frequency: dict[str, float] = {}
        self._chunk_vectors: list[dict[str, float]] = []

    def index(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks
        term_frequencies = [Counter(tokenize(chunk.text)) for chunk in chunks]
        document_frequency: Counter[str] = Counter()
        for term_frequency in term_frequencies:
            document_frequency.update(term_frequency.keys())
        chunk_count = len(chunks)
        self._inverse_document_frequency = {
            term: math.log((chunk_count + 1) / (frequency + 1)) + 1.0
            for term, frequency in document_frequency.items()
        }
        self._chunk_vectors = [
            self._vectorize(term_frequency) for term_frequency in term_frequencies
        ]

    def _vectorize(self, term_frequency: Counter[str]) -> dict[str, float]:
        return _l2_normalize(
            {
                term: count * self._inverse_document_frequency.get(term, 0.0)
                for term, count in term_frequency.items()
            }
        )

    def query(self, text: str, top_k: int = 3) -> list[tuple[Chunk, float]]:
        query_vector = self._vectorize(Counter(tokenize(text)))
        scored = [
            (chunk, _cosine(query_vector, chunk_vector))
            for chunk, chunk_vector in zip(self._chunks, self._chunk_vectors)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]


@dataclass
class Summary:
    """The extractive content summary of an unidentified document."""

    keywords: list[str] = field(default_factory=list)
    key_sentences: list[str] = field(default_factory=list)


def summarize_extractive(
    text: str, top_keywords: int = 8, top_sentences: int = 3
) -> Summary:
    """An extractive summary: salient KEYWORDS + the most representative SENTENCES.

    Keywords are the most frequent content terms (stopword-filtered). Sentences are scored
    by their mean keyword weight (mean, not sum, so a long sentence does not win on length
    alone) and returned in original reading order. Purely extractive — no LLM, no download;
    a generative summary is a later tier behind the same lane.
    """
    term_frequency = Counter(tokenize(text))
    if not term_frequency:
        return Summary()
    keywords = [term for term, _count in term_frequency.most_common(top_keywords)]

    keyword_weight = dict(term_frequency)
    scored_sentences: list[tuple[int, float, str]] = []
    for position, sentence in enumerate(split_sentences(text)):
        sentence_tokens = tokenize(sentence)
        if not sentence_tokens:
            continue
        score = sum(keyword_weight.get(token, 0) for token in sentence_tokens) / len(
            sentence_tokens
        )
        scored_sentences.append((position, score, sentence))

    best = sorted(scored_sentences, key=lambda item: item[1], reverse=True)[
        :top_sentences
    ]
    key_sentences = [sentence for position, _score, sentence in sorted(best)]
    return Summary(keywords=keywords, key_sentences=key_sentences)
