"""Corroboration smoke — the métier-configured attestation roles feed corroborated_by
(no OCR, no llama).

    uv run python corroboration_smoke.py

User decision (2026-07-08): WHICH record fields play holder / issue / expiry is MÉTIER
CONFIG — an `attestation_reference_roles` block traveling with the template (assigned
at promotion through the review page, or curated by hand), never code. On a scratch
store, with synthetic attestation + titre templates staged in D2:

  1. a validated attestation lands in D1 (done/auto, ISO dates on the record);
  2. a titre of the SAME holder issued WITHIN the training window -> corroborated ->
     validated/auto;
  3. a titre of ANOTHER holder (no attestation on file) -> needs_review (pending, not
     rejected — "ma mère peut me faire une certif" never auto-validates, a human
     decides);
  4. a titre of the same holder issued OUTSIDE the window -> needs_review;
  5. the promotion endpoint WRITES the roles block (reviewer's selects) into D2, and
     refuses an incomplete mapping or an unknown field (400) — the human maps among
     the draft's own fields, never invents one.

No PII in this file: fictional names, scratch store/spool.
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_corroboration_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

import api_maquette  # noqa: E402
from draft_smoke import _attestation_lines, _write_pdf  # noqa: E402  (PII-free corpus)
from ocr_bifunction.drafting import (  # noqa: E402
    DraftingDocument,
    cluster_unknown_documents,
    draft_from_cluster,
    seed_candidate_checks,
)
from ocr_bifunction.reader import read_document  # noqa: E402
from ocr_bifunction.repository import Job, SqliteRepository  # noqa: E402
from ocr_bifunction.review_repository import (  # noqa: E402
    Review,
    SqliteReviewRepository,
    Suggestion,
)
from ocr_bifunction.template_repository import SqliteTemplateRepository  # noqa: E402

_ATTESTATION_CORPUS = [
    ("FICTIF Alice", "12/03/2024", "12/03/2027", "H0B0 B1V", "DOSSIER-2024-0117"),
    ("EXEMPLE Bruno", "05/11/2023", "05/11/2026", "B1V BR", "DOSSIER-2023-0492"),
    ("SPECIMEN Chloe", "28/06/2024", "28/06/2027", "H0B0 BR", "DOSSIER-2024-0663"),
]


def _titre_lines(holder: str, issue_date: str, codes: str) -> list:
    """A self-declared titre d'habilitation (employer-issued): same 28pt label->value
    geometry as the attestations, distinct vocabulary so the two cluster apart."""
    return [
        (72, 70, "ENTREPRISE EMPLOYEUR FICTIVE SARL"),
        (72, 100, "TITRE D HABILITATION ELECTRIQUE"),
        (72, 130, "Norme NF C 18-510"),
        (72, 190, "Nom du titulaire"),
        (72, 218, holder),
        (72, 275, "Date d emission"),
        (72, 303, issue_date),
        (72, 360, "Codes attribues"),
        (72, 388, codes),
    ]


_TITRE_CORPUS = [
    ("FICTIF Alice", "10/05/2025", "H0B0 B1V"),
    ("EXEMPLE Bruno", "01/02/2025", "B1V BR"),
    ("SPECIMEN Chloe", "20/09/2025", "H0B0 BR"),
]

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _payload_for(path: Path, document_type: str) -> dict:
    return {
        "files": [
            {
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        ],
        "document_type": document_type,
    }


def _draft_template(
    paths: list[Path], category: str, template_id: str
) -> tuple[dict, dict[str, dict]]:
    """Draft + candidate-seed a template from born-digital docs; return it with the
    per-source extractions (used to locate field names by their known values)."""
    documents = []
    for path in paths:
        result = read_document(path, None)
        documents.append(
            DraftingDocument(source=path.name, text=result.text, lines=result.lines)
        )
    cluster = cluster_unknown_documents(documents)[0]
    report = draft_from_cluster(cluster, category, template_id)
    if report.template is None:
        raise RuntimeError(f"draft failed: {report.reasons}")
    seeded = seed_candidate_checks(report.template, report.extractions_by_source)
    return seeded, report.extractions_by_source


def _field_named_by_value(extractions: dict[str, dict], source: str, value: str) -> str:
    return next(
        name for name, extracted in extractions[source].items() if extracted == value
    )


def run() -> int:
    corpus_directory = _SCRATCH / "corpus"
    corpus_directory.mkdir()
    attestation_paths: list[Path] = []
    for index, values in enumerate(_ATTESTATION_CORPUS):
        path = corpus_directory / f"attestation_{index + 1}.pdf"
        _write_pdf(path, _attestation_lines(*values))
        attestation_paths.append(path)
    titre_paths: list[Path] = []
    for index, values in enumerate(_TITRE_CORPUS):
        path = corpus_directory / f"titre_{index + 1}.pdf"
        _write_pdf(path, _titre_lines(*values))
        titre_paths.append(path)
    late_titre_path = corpus_directory / "titre_hors_fenetre.pdf"
    _write_pdf(late_titre_path, _titre_lines("FICTIF Alice", "01/01/2020", "H0B0"))

    # Attestation template WITH the métier roles block (hand-curated here; the
    # promotion path writes the same block — proven in case 5).
    attestation_template, attestation_extractions = _draft_template(
        attestation_paths, "attestation", "attestation_regime_01"
    )
    attestation_source = attestation_paths[0].name
    attestation_template["attestation_reference_roles"] = {
        "holder_field": _field_named_by_value(
            attestation_extractions, attestation_source, "FICTIF Alice"
        ),
        "issue_date_field": _field_named_by_value(
            attestation_extractions, attestation_source, "12/03/2024"
        ),
        "expiry_date_field": _field_named_by_value(
            attestation_extractions, attestation_source, "12/03/2027"
        ),
    }

    # Titre template WITH the corroborated_by check (employer regime: never auto alone).
    titre_template, titre_extractions = _draft_template(
        titre_paths, "titre_habilitation", "titre_regime_01"
    )
    titre_source = titre_paths[0].name
    titre_holder_field = _field_named_by_value(
        titre_extractions, titre_source, "FICTIF Alice"
    )
    titre_issue_field = _field_named_by_value(
        titre_extractions, titre_source, "10/05/2025"
    )
    titre_template["validation"]["required"] = [
        *titre_template["validation"]["required"],
        {
            "check": "corroborated_by",
            "holder_field": titre_holder_field,
            "issue_field": titre_issue_field,
        },
    ]

    template_repository = SqliteTemplateRepository(os.environ["OCR_STORE_PATH"])
    template_repository.upsert(attestation_template, active=True)
    template_repository.upsert(titre_template, active=True)
    roles_survived_store = template_repository.get("attestation_regime_01").get(
        "attestation_reference_roles"
    )
    template_repository.close()

    with TestClient(api_maquette.app) as client:
        _check(
            "roles block survives the D2 store round-trip",
            roles_survived_store == attestation_template["attestation_reference_roles"],
        )

        # 1. The attestation validates -> a corroborating record ON FILE.
        attestation_upload = client.post(
            "/v1/documents:validate",
            json=_payload_for(attestation_paths[0], "attestation"),
        ).json()
        _check(
            "attestation -> validated/auto (on file, ISO dates)",
            attestation_upload["status"] == "validated",
        )

        # 2. Titre, same holder, issued within the training window -> corroborated.
        corroborated = client.post(
            "/v1/documents:validate",
            json=_payload_for(titre_paths[0], "titre_habilitation"),
        ).json()
        _check(
            "titre same holder within window -> corroborated -> validated/auto",
            corroborated["status"] == "validated" and corroborated["verdict"] == "auto",
        )

        # 3. Titre of another holder (no attestation on file for him) -> review.
        uncorroborated = client.post(
            "/v1/documents:validate",
            json=_payload_for(titre_paths[1], "titre_habilitation"),
        ).json()
        _check(
            "titre without a covering attestation -> needs_review (pending, not reject)",
            uncorroborated["status"] == "needs_review"
            and any("corroborat" in reason for reason in uncorroborated["reasons"]),
        )

        # 4. Titre of the same holder but OUTSIDE the window -> review.
        outside_window = client.post(
            "/v1/documents:validate",
            json=_payload_for(late_titre_path, "titre_habilitation"),
        ).json()
        _check(
            "titre outside the training window -> needs_review",
            outside_window["status"] == "needs_review",
        )

        # 5. The promotion endpoint writes the roles block (and guards it).
        repository = SqliteRepository(os.environ["OCR_STORE_PATH"])
        job_id = repository.save(
            Job(
                source="attestation_1.pdf",
                category_lane="rag",
                status="needs_review",
            )
        )
        repository.close()
        review_repository = SqliteReviewRepository(os.environ["OCR_STORE_PATH"])
        draft_for_promotion = {
            **attestation_template,
            "template_id": "draft_attestation_roles_01",
        }
        draft_for_promotion.pop("attestation_reference_roles")
        review_id = review_repository.open_review(
            Review(
                job_id=job_id,
                projection={"source": "attestation_1.pdf", "lane": "rag"},
                suggestion=Suggestion(
                    template_id="draft_attestation_roles_01",
                    category="attestation",
                    template=draft_for_promotion,
                ),
            )
        )
        review_repository.close()
        roles = attestation_template["attestation_reference_roles"]
        incomplete = client.post(
            f"/v1/suggestions/{review_id}/validate",
            json={"reference_roles": {"holder_field": roles["holder_field"]}},
        )
        unknown_field = client.post(
            f"/v1/suggestions/{review_id}/validate",
            json={"reference_roles": {**roles, "holder_field": "champ_inexistant"}},
        )
        _check(
            "promotion guards: incomplete mapping 400, unknown field 400",
            incomplete.status_code == 400 and unknown_field.status_code == 400,
        )
        promoted = client.post(
            f"/v1/suggestions/{review_id}/validate",
            json={"reference_roles": roles},
        ).json()
        template_repository = SqliteTemplateRepository(os.environ["OCR_STORE_PATH"])
        promoted_row = template_repository.get("draft_attestation_roles_01")
        template_repository.close()
        _check(
            "promotion writes the reviewer's roles block into D2",
            promoted.get("promoted_template_id") == "draft_attestation_roles_01"
            and promoted_row.get("attestation_reference_roles") == roles,
        )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT métier-configured corroboration: {'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
