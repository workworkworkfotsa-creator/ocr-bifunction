"""Lane RAG runner — summarize + index an unidentified document, then retrieve passages.

    uv run python rag_check.py <document>
    uv run python rag_check.py <document> --query "ta question" --top-k 3

For a NON-structured doc (docx memo, article PDF) that matched no structured template, this
lane gives the human a handle on it: a CONTENT SUMMARY (salient keywords + representative
sentences) and a searchable INDEX (cosine top-k passages for a query). No extraction, no
auto-validation — an unidentified doc is for the human by construction; this just makes it
legible and findable. Reads via the same `read_document` (docx native, PDF text layer), so
born-digital docs need no OCR engine.

The retrieval engine is the jettisonable lexical TF-IDF baseline (`rag.TfidfRetriever`); a
semantic embedding retriever swaps in behind the same `Retriever` slot later.

No PII lives in this file: paths/queries come from the command line and content appears only
in the runtime output, never in the repo.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ocr_bifunction.rag import (
    TfidfRetriever,
    chunk_document,
    summarize_extractive,
)
from ocr_bifunction.reader import read_document


def analyze(document_path: Path, query: str | None, top_k: int) -> int:
    result = read_document(document_path)  # docx native / PDF text layer, no OCR engine
    if not result.text.strip():
        print(
            f"{document_path.name}: no extractable text (image-only? needs OCR engine)"
        )
        return 1

    chunks = chunk_document(result.text, source=document_path.name)
    retriever = TfidfRetriever()
    retriever.index(chunks)
    summary = summarize_extractive(result.text)

    print("=" * 64)
    print(f"{document_path.name}  ({result.backend_name})")
    print(f"  {result.character_count} chars, {len(chunks)} chunk(s) indexed")
    print("\n-- summary --")
    print(f"  keywords: {', '.join(summary.keywords) or '(none)'}")
    print("  key sentences:")
    for sentence in summary.key_sentences:
        print(f"    • {sentence}")

    if query:
        print(f"\n-- top {top_k} passages for: {query!r} --")
        for rank, (chunk, score) in enumerate(retriever.query(query, top_k), start=1):
            preview = " ".join(chunk.text.split())
            if len(preview) > 240:
                preview = preview[:240] + "…"
            print(f"  [{rank}] score={score:.3f}  (chunk {chunk.index})")
            print(f"      {preview}")
    print("=" * 64)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize + index a non-structured document, then retrieve passages."
    )
    parser.add_argument("document", type=Path, help="Path to the docx / PDF / image.")
    parser.add_argument(
        "--query",
        default=None,
        help="Optional query: print the top-k matching passages.",
    )
    parser.add_argument(
        "--top-k", type=int, default=3, help="How many passages to return for a query."
    )
    arguments = parser.parse_args()
    return analyze(arguments.document, arguments.query, arguments.top_k)


if __name__ == "__main__":
    raise SystemExit(main())
