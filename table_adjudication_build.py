"""Table adjudication artefact — put the PAGE next to both reconstructions and let a human rule.

    uv run python table_adjudication_build.py --go
    uv run python table_adjudication_build.py --go --limit 2 --max-pages 3

WHY THIS EXISTS: two extractors that disagree cannot establish which one is right — truth is not
derivable from a contradiction. The measured divergence (100% on the first real run) turned out to be
a SEGMENTATION convention difference, not a quality signal, so the only way forward is a small
human-adjudicated reference. This builds the cheapest possible one: for each page, the rendered page
image beside what each method reconstructed. A few minutes of eyeballing settles what no metric could.

THE HYPOTHESIS IT IS MEANT TO TEST (state it before looking, so the result can refute it):
  - pdfplumber works from the PDF's real GEOMETRY (ruling lines, word positions). For a table with
    DRAWN BORDERS the truth is literally in the file, so it should win by construction.
  - Docling's TableFormer INFERS structure. For a table with NO ruling lines (aligned by whitespace)
    geometry has nothing to bite on, so the neural model should win.
If that holds, the rule stops being "which tool is better" and becomes "ruled table -> geometric,
unruled table -> neural", which is operable. If it does not hold, we learned that too.

Note on the geometric side: `pdfplumber` is called DIRECTLY rather than through markitdown. It is the
very engine markitdown uses for tables, but called directly it yields tables PER PAGE with their
bounding boxes — sidestepping the unreliable page separators measured in markitdown's output. Same
reconstruction, better control.

SHARED MACHINE: the Docling pass is heavy, `--go` is required.

PII — READ THIS: the output HTML embeds RENDERED IMAGES OF REAL DOCUMENTS and real cell content. It
is written under `outputs/` (gitignored, verified) and must NEVER be committed, shared, or pasted.
It is a local adjudication aid, nothing else.
"""

from __future__ import annotations

import argparse
import base64
import html
import time
from pathlib import Path

OUTPUT_PATH = Path("outputs/table_adjudication.html")


def _markdown_tables(markdown: str) -> list[list[list[str]]]:
    """Parse the markdown tables of one page into rows of cells (alignment rows dropped)."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if not all(set(cell) <= set(":- ") and cell for cell in cells):
                current.append(cells)
            continue
        if current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    return tables


def _render_table(rows: list[list[str]]) -> str:
    body = "".join(
        "<tr>"
        + "".join(
            f"<td>{html.escape(str(cell) if cell is not None else '')}</td>"
            for cell in row
        )
        + "</tr>"
        for row in rows
    )
    return f"<table>{body}</table>"


def _render_side(title: str, tables: list[list[list[str]]]) -> str:
    if not tables:
        return f"<h4>{html.escape(title)} — <em>aucune table</em></h4>"
    shapes = ", ".join(f"{len(t)}x{max(len(r) for r in t)}" for t in tables)
    blocks = "".join(_render_table(rows) for rows in tables)
    return (
        f"<h4>{html.escape(title)} — {len(tables)} table(s) <span class='shapes'>[{shapes}]</span></h4>"
        f"{blocks}"
    )


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="*", default=["inputs"])
    parser.add_argument("--limit", type=int, default=2, help="documents to adjudicate")
    parser.add_argument("--max-pages", type=int, default=3, help="pages per document")
    parser.add_argument(
        "--go", action="store_true", help="required: runs heavy Docling"
    )
    arguments = parser.parse_args()

    import pdfplumber
    import pymupdf

    documents: list[Path] = []
    for raw in arguments.inputs:
        candidate = Path(raw)
        if candidate.is_dir():
            documents.extend(sorted(p for p in candidate.rglob("*.pdf") if p.is_file()))
        elif candidate.is_file():
            documents.append(candidate)

    # Cheap selector: only documents where the geometric side actually finds tables.
    selected: list[Path] = []
    for path in documents:
        try:
            with pdfplumber.open(path) as pdf:
                if any(
                    page.extract_tables() for page in pdf.pages[: arguments.max_pages]
                ):
                    selected.append(path)
        except Exception:
            continue
        if len(selected) >= arguments.limit:
            break

    if not selected:
        print("No document with tables found.")
        return 1

    if not arguments.go:
        print(
            f"DRY RUN — would adjudicate {len(selected)} document(s). Re-run with --go."
        )
        return 0

    from docling.document_converter import DocumentConverter

    from ocr_bifunction.docling_page_range_converter import convert_document_resiliently

    print(f"Adjudicating {len(selected)} document(s) — heavy Docling pass.\n")
    converter = DocumentConverter()
    sections: list[str] = []

    for document_index, path in enumerate(selected, start=1):
        started = time.perf_counter()
        conversion = convert_document_resiliently(path, converter=converter)
        docling_by_page = {p.page_number: p for p in conversion.page_results}
        print(
            f"  doc{document_index:02d}: docling done in {time.perf_counter() - started:.1f}s"
        )

        with pdfplumber.open(path) as pdf, pymupdf.open(path) as rendered:
            for page_index, plumber_page in enumerate(pdf.pages[: arguments.max_pages]):
                page_number = page_index + 1
                plumber_tables = [
                    [
                        [cell if cell is not None else "" for cell in row]
                        for row in table
                    ]
                    for table in plumber_page.extract_tables()
                ]
                docling_page = docling_by_page.get(page_number)
                docling_tables = (
                    _markdown_tables(docling_page.markdown) if docling_page else []
                )
                if not plumber_tables and not docling_tables:
                    continue

                pixmap = rendered[page_index].get_pixmap(dpi=110)
                encoded = base64.b64encode(pixmap.tobytes("png")).decode("ascii")
                layout = (
                    f"layout_score {docling_page.layout_score:.2f}"
                    if docling_page
                    else "absente"
                )
                sections.append(
                    f"<section><h3>doc{document_index:02d} — page {page_number} "
                    f"<span class='meta'>({layout})</span></h3>"
                    f"<img src='data:image/png;base64,{encoded}' alt='page rendue'>"
                    f"<div class='grid'>"
                    f"<div class='col'>{_render_side('Docling — TableFormer (neural)', docling_tables)}</div>"
                    f"<div class='col'>{_render_side('pdfplumber — geometrique', plumber_tables)}</div>"
                    f"</div></section>"
                )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        "<!doctype html><meta charset='utf-8'><title>Adjudication des tables</title>"
        "<style>"
        "body{font-family:system-ui,sans-serif;margin:2rem;max-width:1400px}"
        ".warn{background:#fee;border:2px solid #c00;padding:1rem;border-radius:6px}"
        "section{border-top:3px solid #333;margin-top:2.5rem;padding-top:1rem}"
        "img{max-width:100%;border:1px solid #ccc;display:block;margin-bottom:1rem}"
        ".grid{display:grid;grid-template-columns:1fr 1fr;gap:1.5rem}"
        ".col{min-width:0;overflow-x:auto}"
        "table{border-collapse:collapse;margin:.5rem 0;font-size:.8rem}"
        "td{border:1px solid #999;padding:2px 6px;vertical-align:top}"
        ".shapes{color:#666;font-weight:normal}.meta{color:#666;font-size:.85rem}"
        "</style>"
        "<h1>Adjudication des tables — Docling vs pdfplumber</h1>"
        "<p class='warn'><strong>PII — LOCAL UNIQUEMENT.</strong> Ce fichier contient des images de "
        "documents reels et le contenu de leurs cellules. Il est ecrit sous <code>outputs/</code> "
        "(gitignore). Ne jamais le committer, le partager ni le coller ailleurs.</p>"
        "<p>Pour chaque page : l'image reelle, puis les deux reconstructions. "
        "<strong>Hypothese a refuter :</strong> table <em>avec bordures</em> &rarr; pdfplumber "
        "(geometrique) devrait gagner ; table <em>sans bordures</em> &rarr; Docling (neural) "
        "devrait gagner.</p>" + "".join(sections),
        encoding="utf-8",
    )
    print(
        f"\nEcrit : {OUTPUT_PATH}  ({OUTPUT_PATH.stat().st_size // 1024} Ko, {len(sections)} page(s))"
    )
    print("Ouvre-le, tranche a l'oeil, et dis-moi ce que tu vois.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
