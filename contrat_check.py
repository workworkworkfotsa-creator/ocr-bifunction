"""Contract store smoke (Étape 1a) — index a CROSS-REFERENCED set of contract PDFs as
ONE corpus, then retrieve verbatim passages WITH provenance (doc, page).

    uv run python contrat_check.py inputs/A.pdf inputs/B.pdf --query "ta question"
    uv run python contrat_check.py inputs/*.pdf --query "..." --engine embedding

Why this exists: the real corpus (a main contract + an avenant + an annexe) cross-
references ACROSS documents ("l'avenant 7 modifie l'article X du contrat"). Single-doc
retrieval cannot see those links, so the documents are indexed TOGETHER. This step proves
the plumbing — read (born-digital, no OCR) -> chunk WITH page+bbox -> index -> retrieve ->
show the passage VERBATIM with its source (doc, page) — and surfaces where flat retrieval
breaks on cross-references (the motivation for the next step: a reference graph).

No LLM, no database: in-memory index, brute-force cosine. No PII in this file — paths and
queries come from the command line; document content appears only in runtime output.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ocr_bifunction.rag import (
    Chunk,
    GgufEmbeddingRetriever,
    Retriever,
    TfidfRetriever,
    chunk_textlines,
)
from ocr_bifunction.reader import read_document


def _build_retriever(engine_name: str) -> Retriever:
    """tfidf = zero-dep lexical baseline (no server); embedding = semantic GGUF."""
    if engine_name == "embedding":
        return GgufEmbeddingRetriever()
    return TfidfRetriever()


def _provenance(chunk: Chunk) -> str:
    """A compact 'doc p.X-Y' locator from the chunk's spans — the link to the source."""
    if not chunk.spans:
        return chunk.source
    pages = sorted({span.page_index for span in chunk.spans})
    # page_index is 0-based internally; show human 1-based page numbers.
    page_label = (
        f"p.{pages[0] + 1}" if len(pages) == 1 else f"p.{pages[0] + 1}-{pages[-1] + 1}"
    )
    return f"{chunk.source} {page_label}"


def build_corpus(document_paths: list[Path], target_tokens: int) -> list[Chunk]:
    """Read every doc (born-digital text layer, no OCR) and chunk it WITH provenance,
    concatenating into one corpus so cross-document references can be retrieved."""
    corpus: list[Chunk] = []
    for document_path in document_paths:
        result = read_document(document_path)  # no OCR engine: born-digital only
        if not result.lines:
            print(
                f"  ! {document_path.name}: no text lines "
                f"({result.backend_name}; image-only would need an OCR engine)"
            )
            continue
        chunks = chunk_textlines(result.lines, document_path.name, target_tokens)
        corpus.extend(chunks)
        print(
            f"  + {document_path.name}: {result.page_count} pages, "
            f"{result.character_count} chars -> {len(chunks)} chunks"
        )
    return corpus


def retrieve(
    document_paths: list[Path],
    query: str,
    top_k: int,
    engine_name: str,
    target_tokens: int,
    preview_chars: int,
) -> int:
    print("=" * 72)
    print(f"corpus: {len(document_paths)} document(s)  [retriever: {engine_name}]")
    corpus = build_corpus(document_paths, target_tokens)
    if not corpus:
        print("no indexable text in any document")
        return 1
    print(f"  total indexed chunks: {len(corpus)}")

    retriever = _build_retriever(engine_name)
    try:
        retriever.index(corpus)
        print(f"\n-- top {top_k} passages for: {query!r} --")
        for rank, (chunk, score) in enumerate(retriever.query(query, top_k), start=1):
            preview = " ".join(chunk.text.split())
            if len(preview) > preview_chars:
                preview = preview[:preview_chars] + "…"
            print(f"\n  [{rank}] score={score:.3f}  <- {_provenance(chunk)}")
            print(f"      {preview}")
    finally:
        close = getattr(retriever, "close", None)
        if callable(close):
            close()  # stop the embedding server if one was started
    print("\n" + "=" * 72)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Index cross-referenced contract PDFs as one corpus; retrieve "
        "verbatim passages with provenance (doc, page)."
    )
    parser.add_argument("documents", type=Path, nargs="+", help="Contract PDF paths.")
    parser.add_argument("--query", required=True, help="The question to retrieve for.")
    parser.add_argument("--top-k", type=int, default=5, help="Passages to return.")
    parser.add_argument(
        "--engine",
        choices=["tfidf", "embedding"],
        default="tfidf",
        help="Lexical TF-IDF (default, no server) or semantic GGUF embeddings.",
    )
    parser.add_argument(
        "--target-tokens",
        type=int,
        default=160,
        help="Approx content tokens per chunk (clause-ish granularity).",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=320,
        help="Max characters of each passage to print.",
    )
    arguments = parser.parse_args()
    return retrieve(
        arguments.documents,
        arguments.query,
        arguments.top_k,
        arguments.engine,
        arguments.target_tokens,
        arguments.preview_chars,
    )


if __name__ == "__main__":
    raise SystemExit(main())
