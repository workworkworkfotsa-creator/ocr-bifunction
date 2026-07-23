"""Structural smoke for the drafting lane (D-a/D-b) — synthetic, PII-free, no OCR.

Generates a small born-digital corpus with PyMuPDF (obviously fictional holder names:
FICTIF / EXEMPLE / SPECIMEN / DEMO / TEST), then proves on it:
  1. clustering separates the two layouts and leaves the one-off letter alone;
  2. the draft's match anchors are structural vocabulary only — NEVER a holder name
     or a date (the cross-document invariance filter, mechanical PII guard);
  3. the draft re-tests green (match + extract + validate) on every cluster member;
  4. the extracted field values VARY across documents (values, not structure);
  5. the attestation draft does NOT match a certificate document (negative control).

Runs in milliseconds on the text layer — safe while the shared machine is busy. The
real-corpus oracle (scanned attestations, RapidOCR) is draft_check.py and waits for
an explicit GO.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pymupdf

from ocr_bifunction.knowledge.drafting import (
    DraftingDocument,
    cluster_unknown_documents,
    draft_from_cluster,
)
from ocr_bifunction.reading.reader import read_document
from ocr_bifunction.extraction.template import match_template

# --- synthetic corpus ------------------------------------------------------------
# Label placed 28pt above its value (11pt font): far enough apart that PyMuPDF block
# detection keeps them as SEPARATE blocks (the geometry path needs distinct lines),
# close enough that "below, same column" picks the value and not the next label group.

_ATTESTATION_HOLDERS = ["FICTIF Alice", "EXEMPLE Bruno", "SPECIMEN Chloe"]
_ATTESTATION_ISSUE_DATES = ["12/03/2024", "05/11/2023", "28/06/2024"]
_ATTESTATION_EXPIRY_DATES = ["12/03/2027", "05/11/2026", "28/06/2027"]
_ATTESTATION_CODES = ["H0B0 B1V", "H0B0 BR", "B2V BC"]
# Glued "label : value" line — exercises the colon-prefix pattern family (the shape
# born-digital blocks produce, cf. the real attestation cluster).
_ATTESTATION_REFERENCES = [
    "DOSSIER-2024-0117",
    "DOSSIER-2023-0492",
    "DOSSIER-2024-0663",
]

_CERTIFICATE_NAMES = ["DEMO Karim", "TEST Nadia"]
_CERTIFICATE_VISIT_DATES = ["02/05/2026", "17/04/2026"]

_LETTER_TEXT = (
    "Objet: reclamation concernant la livraison du materiel informatique commande "
    "en mars. Nous constatons un retard important sur la commande referencee "
    "ci-dessus et vous demandons de proceder a la livraison sous quinzaine."
)


def _write_pdf(path: Path, positioned_lines: list[tuple[float, float, str]]) -> None:
    document = pymupdf.open()
    page = document.new_page()
    for x_position, y_position, text in positioned_lines:
        page.insert_text((x_position, y_position), text, fontsize=11)
    document.save(path)
    document.close()


def _attestation_lines(
    holder: str, issue_date: str, expiry_date: str, codes: str, reference: str
) -> list[tuple[float, float, str]]:
    return [
        (72, 70, "CENTRE DE FORMATION SPECIMEN SAS"),
        (72, 100, "ATTESTATION DE FORMATION"),
        (72, 130, "Habilitation electrique NF C 18-510"),
        (72, 190, "Nom du titulaire"),
        (72, 218, holder),
        (72, 275, "Delivree le"),
        (72, 303, issue_date),
        (72, 360, "Valable jusqu'au"),
        (72, 388, expiry_date),
        (72, 445, "Codes obtenus"),
        (72, 473, codes),
        (72, 530, f"Reference du dossier : {reference}"),
    ]


def _certificate_lines(
    salarie_name: str, visit_date: str
) -> list[tuple[float, float, str]]:
    return [
        (72, 70, "CABINET MEDICAL FICTIF"),
        (72, 100, "CERTIFICAT D APTITUDE MEDICALE"),
        (72, 160, "Nom du salarie"),
        (72, 188, salarie_name),
        (72, 245, "Date de visite"),
        (72, 273, visit_date),
        (72, 330, "Apte sans restriction"),
    ]


def _build_corpus(directory: Path) -> list[Path]:
    paths: list[Path] = []
    for index in range(3):
        path = directory / f"attestation_{index + 1}.pdf"
        _write_pdf(
            path,
            _attestation_lines(
                _ATTESTATION_HOLDERS[index],
                _ATTESTATION_ISSUE_DATES[index],
                _ATTESTATION_EXPIRY_DATES[index],
                _ATTESTATION_CODES[index],
                _ATTESTATION_REFERENCES[index],
            ),
        )
        paths.append(path)
    for index in range(2):
        path = directory / f"certificat_{index + 1}.pdf"
        _write_pdf(
            path,
            _certificate_lines(
                _CERTIFICATE_NAMES[index], _CERTIFICATE_VISIT_DATES[index]
            ),
        )
        paths.append(path)
    letter_path = directory / "courrier.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(pymupdf.Rect(72, 72, 520, 400), _LETTER_TEXT, fontsize=11)
    document.save(letter_path)
    document.close()
    paths.append(letter_path)
    return paths


# --- assertions -------------------------------------------------------------------

_checks_passed = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _checks_passed
    if not condition:
        raise AssertionError(f"CHECK FAILED: {label} {detail}")
    _checks_passed += 1
    print(f"  PASS {label}")


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        corpus_paths = _build_corpus(Path(temporary_directory))

        print("=== read (text layer only, no OCR) ===")
        documents: list[DraftingDocument] = []
        for path in corpus_paths:
            result = read_document(path)  # no OCR engine on purpose
            print(
                f"  {path.name}: backend={result.backend_name} "
                f"lines={len(result.lines)}"
            )
            documents.append(
                DraftingDocument(source=path.name, text=result.text, lines=result.lines)
            )
        _check(
            "no document required OCR",
            all(document.text for document in documents),
        )
        _check(
            "born-digital blocks kept label and value on separate lines",
            len(documents[0].lines) == 12,
            f"(got {len(documents[0].lines)} lines, expected 12)",
        )

        print("\n=== D-a clustering ===")
        clusters = cluster_unknown_documents(documents)
        composition = sorted(
            tuple(sorted(document.source for document in cluster))
            for cluster in clusters
        )
        expected_composition = sorted(
            [
                ("attestation_1.pdf", "attestation_2.pdf", "attestation_3.pdf"),
                ("certificat_1.pdf", "certificat_2.pdf"),
                ("courrier.pdf",),
            ]
        )
        _check(
            "2 layout clusters + 1 singleton, exact composition",
            composition == expected_composition,
            f"(got {composition})",
        )

        attestation_cluster = next(
            cluster
            for cluster in clusters
            if cluster[0].source.startswith("attestation")
        )
        certificate_cluster = next(
            cluster
            for cluster in clusters
            if cluster[0].source.startswith("certificat")
        )

        print("\n=== D-b draft: attestation cluster ===")
        attestation_report = draft_from_cluster(
            attestation_cluster, "attestation", "draft_attestation_01"
        )
        _check("attestation draft accepted", attestation_report.template is not None)
        draft = attestation_report.template
        assert draft is not None
        print(f"  anchors: {draft['match']['all_anchors']}")
        for field_entry in draft["fields"]:
            print(f"  field: {field_entry}")

        anchor_material = " ".join(attestation_report.anchors).lower()
        # Every per-document value is a leak candidate — including the references:
        # a fuzzy invariance predicate once let "label : VALUE" through as an anchor.
        personal_values = (
            _ATTESTATION_HOLDERS
            + _ATTESTATION_ISSUE_DATES
            + _ATTESTATION_EXPIRY_DATES
            + _ATTESTATION_CODES
            + _ATTESTATION_REFERENCES
        )
        _check(
            "no per-document value leaked into the match anchors",
            all(value.lower() not in anchor_material for value in personal_values),
            f"(anchors: {attestation_report.anchors})",
        )
        field_names = {field_entry["name"] for field_entry in draft["fields"]}
        _check(
            "the 4 geometry zones + the glued colon line became fields",
            field_names
            == {
                "nom_du_titulaire",
                "delivree_le",
                "valable_jusqu_au",
                "codes_obtenus",
                "reference_du_dossier",
            },
            f"(got {sorted(field_names)})",
        )
        reference_field = next(
            field_entry
            for field_entry in draft["fields"]
            if field_entry["name"] == "reference_du_dossier"
        )
        reference_values = {
            extraction["reference_du_dossier"]
            for extraction in attestation_report.extractions_by_source.values()
        }
        _check(
            "glued line -> PATTERN field, values extracted and varying",
            "pattern" in reference_field
            and reference_values == set(_ATTESTATION_REFERENCES),
            f"(field={reference_field}, values={reference_values})",
        )
        holder_values = {
            extraction["nom_du_titulaire"]
            for extraction in attestation_report.extractions_by_source.values()
        }
        _check(
            "holder values extracted and VARYING across the cluster",
            holder_values == set(_ATTESTATION_HOLDERS),
            f"(got {holder_values})",
        )
        _check(
            "draft proposes presence checks for every kept field (candidates)",
            {rule["field"] for rule in draft["validation"]["required"]} == field_names,
        )

        print("\n=== D-b draft: certificate cluster (minimum size 2) ===")
        certificate_report = draft_from_cluster(
            certificate_cluster, "certificat_medical", "draft_certificat_01"
        )
        _check("certificate draft accepted", certificate_report.template is not None)
        certificate_draft = certificate_report.template
        assert certificate_draft is not None
        certificate_field_names = {
            field_entry["name"] for field_entry in certificate_draft["fields"]
        }
        _check(
            "certificate fields = the 2 variable zones",
            certificate_field_names == {"nom_du_salarie", "date_de_visite"},
            f"(got {sorted(certificate_field_names)})",
        )

        print("\n=== negative control ===")
        _check(
            "attestation draft does NOT match a certificate document",
            match_template(certificate_cluster[0].lines, [draft]) is None,
        )

        print(f"\nSMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
