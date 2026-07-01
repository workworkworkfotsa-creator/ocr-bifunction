"""Contract reference graph smoke (Étape 2) — build the reference graph over a cross-
referenced contract corpus, then answer a query with a 1-HOP TRAVERSAL, not just retrieval.

    uv run python contrat_graph_check.py inputs/A.pdf inputs/B.pdf --query "que modifie l'avenant 7"

Étape 1 proved retrieval FINDS the modifying clause; it did not RESOLVE the link the clause
describes. This runner adds that: read (born-digital, no OCR) -> segment into articles ->
ONE LLM call per article extracts its outgoing reference edges -> resolve each edge to a
node (or mark it dangling) -> retrieve the relevant article(s) -> follow their edges 1 hop
and print the source clause + its targets. Oracle = the real run on "que modifie l'avenant 7".

Cost warning: this launches granite-4.0-h-tiny (llama-server child, ~4 GB RAM) and makes ONE
call per article chunk. Batch/nightly territory — do NOT run it while another heavy task is
using the machine. Retrieval defaults to in-process TF-IDF so ONLY the generator server runs.

No PII in this file — paths and queries come from the command line; document content appears
only in runtime output.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ocr_bifunction.generation import LlamaCppGenerator, Reference
from ocr_bifunction.rag import (
    Chunk,
    GgufEmbeddingRetriever,
    Retriever,
    TfidfRetriever,
    segment_articles,
)
from ocr_bifunction.reader import read_document
from ocr_bifunction.reference_graph import ReferenceEdge, build_reference_graph


def _provenance(chunk: Chunk) -> str:
    """A compact 'heading — doc p.X-Y' locator from the chunk's spans (link to source)."""
    if not chunk.spans:
        return chunk.heading or chunk.source
    pages = sorted({span.page_index for span in chunk.spans})
    page_label = (
        f"p.{pages[0] + 1}" if len(pages) == 1 else f"p.{pages[0] + 1}-{pages[-1] + 1}"
    )
    locator = f"{chunk.source} {page_label}"
    return f"{chunk.heading}  —  {locator}" if chunk.heading else locator


def build_corpus(document_paths: list[Path], target_tokens: int) -> list[Chunk]:
    """Read every doc (born-digital text layer, no OCR) and segment it into article chunks,
    concatenating into one corpus so cross-document references live in one graph."""
    corpus: list[Chunk] = []
    for document_path in document_paths:
        result = read_document(document_path)  # no OCR engine: born-digital only
        if not result.lines:
            print(f"  ! {document_path.name}: no text lines ({result.backend_name})")
            continue
        chunks = segment_articles(result.lines, document_path.name, target_tokens)
        corpus.extend(chunks)
        print(
            f"  + {document_path.name}: {result.page_count} pages -> {len(chunks)} article chunks"
        )
    return corpus


def _endpoint(label: str, text: str | None, target: Chunk | None) -> str | None:
    """Render one edge endpoint: the cited string + whether it resolved to a node."""
    if text is None:
        return None
    if target is not None:
        return f"{label}={text!r}  ->  RESOLVED: {target.heading} ({target.source})"
    return f"{label}={text!r}  ->  DANGLING (not a node in the corpus)"


def _print_edges(edges: list[ReferenceEdge], indent: str = "      ") -> None:
    for edge in edges:
        print(f"{indent}[{edge.reference.relation}]")
        ancien = _endpoint("ancien", edge.reference.ancien, edge.ancien_target)
        nouveau = _endpoint("nouveau", edge.reference.nouveau, edge.nouveau_target)
        if ancien:
            print(f"{indent}  {ancien}")
        if nouveau:
            print(f"{indent}  {nouveau}")


def _build_retriever(engine_name: str) -> Retriever:
    if engine_name == "embedding":
        return GgufEmbeddingRetriever()
    return TfidfRetriever()


def run(
    document_paths: list[Path],
    query: str,
    top_k: int,
    engine_name: str,
    target_tokens: int,
    binary_path: str | None,
    model_path: str | None,
    threads: int,
) -> int:
    print("=" * 72)
    print(f"corpus: {len(document_paths)} document(s)")
    corpus = build_corpus(document_paths, target_tokens)
    if not corpus:
        print("no indexable text in any document")
        return 1
    print(f"  total article chunks: {len(corpus)}")

    # --- Build the reference graph (ONE llama-server child; one LLM call per article). ---
    print(f"\n-- extracting reference edges ({len(corpus)} LLM calls; granite) --")

    def _progress(chunk: Chunk, references: list[Reference]) -> None:
        if references:
            print(f"  {chunk.heading}: {len(references)} edge(s)")

    with LlamaCppGenerator(
        binary_path=binary_path, model_path=model_path, threads=threads
    ) as generator:
        graph = build_reference_graph(corpus, generator, on_progress=_progress)
    total_dangling = sum(
        edge.ancien_dangling + edge.nouveau_dangling for edge in graph.edges
    )
    print(f"  graph: {len(graph.edges)} edges, {total_dangling} dangling endpoint(s)")

    # --- Retrieve, then follow edges 1 hop (retriever server, if any, closed after query). ---
    retriever = _build_retriever(engine_name)
    try:
        retriever.index(corpus)
        retrieved = retriever.query(query, top_k)
    finally:
        close = getattr(retriever, "close", None)
        if callable(close):
            close()

    print(f"\n-- 1-hop traversal for: {query!r}  [retriever: {engine_name}] --")
    for rank, (chunk, score) in enumerate(retrieved, start=1):
        edges = graph.outgoing(chunk)
        print(f"\n  [{rank}] score={score:.3f}  <- {_provenance(chunk)}")
        if not edges:
            print("      (no outgoing reference edges)")
            continue
        print(f"      creates {len(edges)} reference edge(s):")
        _print_edges(edges)
    print("\n" + "=" * 72)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a contract reference graph (LLM edge extraction) and answer a "
        "query by 1-hop traversal from the retrieved article(s)."
    )
    parser.add_argument("documents", type=Path, nargs="+", help="Contract PDF paths.")
    parser.add_argument("--query", required=True, help="The question to retrieve for.")
    parser.add_argument(
        "--top-k", type=int, default=3, help="Articles to traverse from."
    )
    parser.add_argument(
        "--engine",
        choices=["tfidf", "embedding"],
        default="tfidf",
        help="Retriever: in-process TF-IDF (default, no 2nd server) or semantic GGUF.",
    )
    parser.add_argument(
        "--target-tokens",
        type=int,
        default=1200,
        help="Max content tokens per article chunk (article-level: ~1 chunk/article, ~1 LLM call each).",
    )
    parser.add_argument("--binary", default=None, help="Override llama-server path.")
    parser.add_argument("--model", default=None, help="Override generation GGUF path.")
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="llama-server CPU threads: match PHYSICAL cores (compute-bound matmul gains "
        "nothing from hyperthreading; oversubscribing contends). This machine = 4 physical; "
        "use 3 to leave a core for the OS / other tasks (the llama-swap config uses -t 3).",
    )
    arguments = parser.parse_args()
    return run(
        arguments.documents,
        arguments.query,
        arguments.top_k,
        arguments.engine,
        arguments.target_tokens,
        arguments.binary,
        arguments.model,
        arguments.threads,
    )


if __name__ == "__main__":
    raise SystemExit(main())
