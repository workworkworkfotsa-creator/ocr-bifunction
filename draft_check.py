"""D-a/D-b runner — cluster unknown documents and draft templates from invariance.

Usage:
    uv run python draft_check.py <doc paths...> [--category attestation]
        [--threshold 0.5] [--min-cluster-size 2] [--ocr] [--json-out DIR]

Documents are passed on the CLI because D1 does not retain an unknown's path or text
(only the source file name): the drafting lane re-reads the documents itself.

Scanned/image documents need OCR, and OCR costs real CPU on the shared machine — so
OCR is OPT-IN (--ocr): without it the runner FAILS LOUD (exit 2) instead of silently
burning CPU next to another workload.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ocr_bifunction.drafting import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DraftingDocument,
    DraftReport,
    cluster_unknown_documents,
    draft_from_cluster,
    pairwise_similarity,
)
from ocr_bifunction.reader import read_document


def _read_drafting_documents(
    document_paths: list[Path], use_ocr: bool
) -> list[DraftingDocument]:
    ocr_engine = None
    if use_ocr:
        from ocr_bifunction.rapidocr_engine import RapidOcrEngine

        ocr_engine = RapidOcrEngine()

    documents: list[DraftingDocument] = []
    needs_ocr_sources: list[str] = []
    for document_path in document_paths:
        result = read_document(document_path, ocr_engine=ocr_engine)
        if result.needs_ocr:
            needs_ocr_sources.append(document_path.name)
            continue
        print(
            f"  read {document_path.name}: backend={result.backend_name} "
            f"lines={len(result.lines)} chars={result.character_count}"
        )
        documents.append(
            DraftingDocument(
                source=document_path.name, text=result.text, lines=result.lines
            )
        )
    if needs_ocr_sources:
        print(
            "\nOCR GATE: these documents are image-only and need OCR: "
            + ", ".join(needs_ocr_sources)
        )
        print(
            "OCR costs real CPU on the shared machine. Re-run with --ocr "
            "AFTER an explicit GO (cf. HANDOFF: VRP stress test)."
        )
        sys.exit(2)
    return documents


def _print_clusters(
    clusters: list[list[DraftingDocument]], documents: list[DraftingDocument]
) -> None:
    matrix = pairwise_similarity(documents)
    print(
        f"\n=== D-a clustering: {len(documents)} documents -> "
        f"{len(clusters)} clusters ==="
    )
    for cluster_index, cluster in enumerate(clusters, start=1):
        if len(cluster) == 1:
            print(f"  singleton: {cluster[0].source} (one-off, stays RAG)")
        else:
            sources = ", ".join(document.source for document in cluster)
            print(f"  cluster {cluster_index} ({len(cluster)} docs): {sources}")
    print("\n  pairwise similarity:")
    for row_index, document in enumerate(documents):
        row = " ".join(f"{value:.2f}" for value in matrix[row_index])
        print(f"    {document.source:40s} {row}")


def _print_draft_report(template_id: str, report: DraftReport) -> None:
    print(f"\n--- {template_id} ---")
    if report.anchors:
        print("  match anchors:")
        for anchor in report.anchors:
            print(f'    - "{anchor}"')
    if report.template is not None:
        print("  fields (kept by the re-test gate):")
        for field_entry in report.template["fields"]:
            values = " | ".join(
                str(extraction.get(field_entry["name"]))
                for extraction in report.extractions_by_source.values()
            )
            print(
                f"    - {field_entry['name']}  "
                f'[{field_entry["direction"]} of "{field_entry["anchor"]}"]  '
                f"values: {values}"
            )
    for dropped in report.dropped_fields:
        print(f"  dropped: {dropped}")
    if report.template is None:
        print("  verdict: DRAFT REJECTED")
        for reason in report.reasons:
            print(f"    - {reason}")
    else:
        document_count = len(report.extractions_by_source)
        print(
            f"  verdict: DRAFT OK (matches + extracts + validates on "
            f"{document_count}/{document_count} cluster documents)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cluster unknown documents and draft templates from invariance."
    )
    parser.add_argument("documents", nargs="+", type=Path)
    parser.add_argument("--category", default="inconnu")
    parser.add_argument("--threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD)
    parser.add_argument("--min-cluster-size", type=int, default=2)
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="opt-in OCR for image-only documents (real CPU cost, shared machine)",
    )
    parser.add_argument(
        "--json-out", type=Path, default=None, help="directory for the draft JSONs"
    )
    arguments = parser.parse_args()

    print(f"=== reading {len(arguments.documents)} documents ===")
    documents = _read_drafting_documents(arguments.documents, arguments.ocr)
    if not documents:
        print("no readable document")
        sys.exit(2)

    clusters = cluster_unknown_documents(documents, arguments.threshold)
    _print_clusters(clusters, documents)

    draft_index = 0
    for cluster in clusters:
        if len(cluster) < arguments.min_cluster_size:
            continue
        draft_index += 1
        template_id = f"draft_{arguments.category}_{draft_index:02d}"
        report = draft_from_cluster(cluster, arguments.category, template_id)
        _print_draft_report(template_id, report)
        if report.template is not None and arguments.json_out is not None:
            arguments.json_out.mkdir(parents=True, exist_ok=True)
            output_path = arguments.json_out / f"{template_id}.json"
            output_path.write_text(
                json.dumps(report.template, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"  draft written: {output_path}")
    if draft_index == 0:
        print("\nno cluster reached the minimum size — nothing to draft")


if __name__ == "__main__":
    main()
