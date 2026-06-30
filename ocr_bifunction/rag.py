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

import json
import math
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ocr_bifunction.reader import TextLine

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
class ProvenanceSpan:
    """Where one piece of a chunk came from in the SOURCE document: page + box.

    This is the link from the read text back to the original document — so a retrieved
    clause can be shown verbatim AND located (page, bounding box) for the human to verify
    against the source. bbox is (x0, y0, x1, y1) in the page coordinate system.
    """

    page_index: int
    bbox: tuple[float, float, float, float]


@dataclass
class Chunk:
    """One indexed passage: its text, where it came from (source + ordinal), and the
    provenance spans (page + bbox) of every source block it was packed from."""

    text: str
    source: str
    index: int
    spans: list[ProvenanceSpan] = field(default_factory=list)
    # The legal heading this chunk belongs to (e.g. "Article VI. RECEPTION"), when the
    # document has article structure. None for flat (non-article) packing. This is the
    # natural node label for a later reference graph (avenant -> MODIFIES -> article).
    heading: str | None = None


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


def chunk_textlines(
    lines: list[TextLine], source: str, target_tokens: int = 120
) -> list[Chunk]:
    """Pack read TextLines into chunks while KEEPING provenance (page + bbox).

    Same greedy packing as `chunk_document`, but the input carries geometry: each chunk
    records the `ProvenanceSpan` of every block it absorbed, so a retrieved passage links
    back to its exact place in the original document. Born-digital PDFs give one block per
    TextLine; a chunk straddling a page boundary simply records spans on both pages.
    """
    chunks: list[Chunk] = []
    current_parts: list[str] = []
    current_spans: list[ProvenanceSpan] = []
    current_token_count = 0
    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        current_parts.append(text)
        current_spans.append(ProvenanceSpan(line.page_index, line.bbox))
        current_token_count += len(tokenize(text))
        if current_token_count >= target_tokens:
            chunks.append(
                Chunk("\n".join(current_parts), source, len(chunks), current_spans)
            )
            current_parts, current_spans, current_token_count = [], [], 0
    if current_parts:
        chunks.append(
            Chunk("\n".join(current_parts), source, len(chunks), current_spans)
        )
    return chunks


# An article heading: "Article" + a roman OR arabic id, then an optional separator
# (. : ) – ) and the title. Covers both observed schemes — "Article VI. RECEPTION"
# (roman, ENGIE contract) and "Article 2 Modifications introduites par l'Avenant"
# (arabic, avenant). The id is captured to fold a table-of-contents duplicate onto the
# real article (same id -> keep the occurrence with the larger body).
_ARTICLE_HEADING_PATTERN = re.compile(
    r"^\s*(?:ARTICLE|Article)\s+([IVXLC]+|\d+)\b[\s.:)–-]*(.*)$"
)


def _explode_block_lines(lines: list[TextLine]) -> list[TextLine]:
    """Split each block-level TextLine into its constituent text lines, each keeping the
    block's page + bbox. PyMuPDF groups several lines into one block, which can BURY an
    article heading mid-block; exploding lets heading detection (anchored at line start)
    see it. Provenance stays at block-bbox resolution — enough to locate the passage."""
    exploded: list[TextLine] = []
    for line in lines:
        for piece in line.text.split("\n"):
            stripped = piece.strip()
            if stripped:
                exploded.append(
                    TextLine(stripped, line.bbox, line.confidence, line.page_index)
                )
    return exploded


def segment_articles(
    lines: list[TextLine], source: str, max_tokens: int = 400
) -> list[Chunk]:
    """Segment a contract into ARTICLE-level chunks (the node a reference graph needs).

    Splits on article headings (roman or arabic). A table of contents repeats every
    heading with no body, so the same article id is folded onto the occurrence with the
    LARGER body (the real article, not the TOC entry). An article longer than max_tokens
    is sub-packed (so no chunk overflows the embedder's context). Each chunk carries its
    article heading and page+bbox provenance. Falls back to flat `chunk_textlines` when
    the document has no article structure (e.g. an annexe of numbered sections).
    """
    lines = _explode_block_lines(lines)
    boundaries: list[tuple[int, str, str]] = []
    for line_index, line in enumerate(lines):
        match = _ARTICLE_HEADING_PATTERN.match(line.text.strip())
        if match:
            boundaries.append((line_index, match.group(1), match.group(2).strip()))
    if len(boundaries) < 2:
        return chunk_textlines(lines, source, max_tokens)

    # Fold each article id onto its largest-body occurrence (drops TOC duplicates).
    sections: dict[str, tuple[str, list[TextLine], int]] = {}
    for position, (line_index, article_id, title) in enumerate(boundaries):
        end = (
            boundaries[position + 1][0]
            if position + 1 < len(boundaries)
            else len(lines)
        )
        body = lines[line_index:end]
        body_tokens = sum(len(tokenize(line.text)) for line in body)
        heading = f"Article {article_id}" + (f" {title}" if title else "")
        existing = sections.get(article_id)
        if existing is None or body_tokens > existing[2]:
            sections[article_id] = (heading, body, body_tokens)

    chunks: list[Chunk] = []
    for heading, body, _body_tokens in sections.values():
        for sub_chunk in chunk_textlines(body, source, max_tokens):
            sub_chunk.index = len(chunks)
            sub_chunk.heading = heading
            chunks.append(sub_chunk)
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


# --- Semantic retriever: GGUF embeddings via llama.cpp's llama-server. ----------------
# The second `Retriever` impl, swapped in behind the same slot — no torch, reuses the
# existing llama.cpp binary. Config verified against a working sibling project
# (Personal Assistant, 2026-06-29): the build ships no `llama-embedding` CLI, so embeddings
# come from `llama-server --embedding` over its OpenAI-compatible /v1/embeddings endpoint.

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_EMBEDDING_MODEL = (
    _REPO_ROOT / "models" / "granite-embedding-311M-multilingual-r2-Q8_0.gguf"
)
# The llama.cpp server this POC was proven on. IT overrides via RAG_EMBEDDING_BINARY.
_DEFAULT_EMBEDDING_BINARY = (
    r"C:\Users\filipeparente\Tools\llamacpp\b9542\llama-server.exe"
)
# granite-embedding's native context limit is 512 tokens; chunk_document targets ~120
# content tokens, well under, so chunks are not truncated. Texts are still capped before
# embedding as a belt-and-braces guard against an over-long passage erroring the server.
_EMBEDDING_CONTEXT_SIZE = 512
_EMBEDDING_CHARACTER_BUDGET = 1600


def _l2_normalize_dense(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def _dense_dot(left: list[float], right: list[float]) -> float:
    # Both vectors are L2-normalized, so the dot product IS the cosine similarity.
    return sum(x * y for x, y in zip(left, right))


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class GgufEmbeddingRetriever:
    """Semantic retriever backed by a GGUF embedding model served by llama-server.

    Disposable adapter (IT swaps binary / model / port via constructor args or env:
    RAG_EMBEDDING_BINARY / RAG_EMBEDDING_MODEL). The server is started lazily on first
    embed and stopped on close(); use it as a context manager so the process never leaks.
    Vectors come back L2-normalized (llama-server --embd-normalize default 2), so ranking
    is a plain dot product. Proven flags mirror the sibling project: -c 512, CPU, --mmap.
    """

    name = "gguf-embedding"

    def __init__(
        self,
        *,
        binary_path: str | os.PathLike[str] | None = None,
        model_path: str | os.PathLike[str] | None = None,
        host: str = "127.0.0.1",
        port: int | None = None,
        threads: int = 4,
        startup_timeout_seconds: float = 180.0,
    ) -> None:
        self._binary_path = Path(
            binary_path
            or os.environ.get("RAG_EMBEDDING_BINARY", _DEFAULT_EMBEDDING_BINARY)
        )
        self._model_path = Path(
            model_path
            or os.environ.get("RAG_EMBEDDING_MODEL", str(_DEFAULT_EMBEDDING_MODEL))
        )
        self._host = host
        self._port = port
        self._threads = threads
        self._startup_timeout_seconds = startup_timeout_seconds
        self._process: subprocess.Popen[bytes] | None = None
        self._chunks: list[Chunk] = []
        self._chunk_vectors: list[list[float]] = []

    def __enter__(self) -> GgufEmbeddingRetriever:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def _base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def _ensure_server(self) -> None:
        if self._process is not None:
            return
        if self._port is None:
            self._port = _find_free_port()
        self._process = subprocess.Popen(
            [
                str(self._binary_path),
                "-m",
                str(self._model_path),
                "--host",
                self._host,
                "--port",
                str(self._port),
                "-c",
                str(_EMBEDDING_CONTEXT_SIZE),
                "-t",
                str(self._threads),
                "-ngl",
                "0",
                "--mmap",
                "--embedding",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + self._startup_timeout_seconds
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError("llama-server exited before becoming ready")
            try:
                with urllib.request.urlopen(
                    f"{self._base_url()}/health", timeout=2
                ) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError):
                pass  # not up yet
            time.sleep(0.5)
        self.close()
        raise RuntimeError("llama-server did not become ready in time")

    def _embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure_server()
        capped = [text[:_EMBEDDING_CHARACTER_BUDGET] for text in texts]
        payload = json.dumps({"model": "granite-embed", "input": capped}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            f"{self._base_url()}/v1/embeddings",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read())["data"]
        # llama-server returns one item per input; order by "index" to be safe.
        data.sort(key=lambda item: item.get("index", 0))
        return [_l2_normalize_dense(item["embedding"]) for item in data]

    def index(self, chunks: list[Chunk]) -> None:
        self._chunks = chunks
        self._chunk_vectors = (
            self._embed([chunk.text for chunk in chunks]) if chunks else []
        )

    def query(self, text: str, top_k: int = 3) -> list[tuple[Chunk, float]]:
        if not self._chunks:
            return []
        query_vector = self._embed([text])[0]
        scored = [
            (chunk, _dense_dot(query_vector, chunk_vector))
            for chunk, chunk_vector in zip(self._chunks, self._chunk_vectors)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]

    def close(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None
