"""Severity smoke — the per-check métier severity knob (no OCR, no llama).

    uv run python severity_smoke.py

User decision (2026-07-12, « à construire ») : the failure class of a check is métier
config — a rule may carry `"severity": "reject" | "review"` to harden or soften its
DETERMINED failures (e.g. once the registry is trusted: « émetteur ≠ Y → non valide »).
The input-vs-preuve doctrine survives it: an UNDETERMINED failure (absent registry,
unreadable input) stays review whatever the config says. On a scratch store, with a
synthetic attestation template carrying an issuer field (SIRET in text):

  1. HARDEN: issuer_registry + severity=reject, registry lists ANOTHER organism ->
     the read issuer is out of the registry (determined) -> REJECTED (was review);
  2. FAIL-LOUD INVINCIBLE: registry EMPTY -> undetermined -> needs_review DESPITE the
     reject severity;
  3. recognized issuer -> validated/auto;
  4. SOFTEN: vocabulary (rejecting by default) + severity=review -> an invented code
     routes to review instead of rejected (control run without severity: rejected);
  5. CONFIG TYPO: an unknown severity value surfaces as a review reason (fail-loud),
     even when every check passes;
  6. PROMOTION: a ticked candidate may carry a severity (written to D2); an unknown
     severity value at promotion -> 400.

No PII in this file: fictional names + a fictional SIRET, scratch store/spool.
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent

_SCRATCH = Path(tempfile.mkdtemp(prefix="ocr_bifunction_severity_smoke_"))
os.environ["OCR_STORE_PATH"] = str(_SCRATCH / "smoke_store.sqlite")
os.environ["OCR_SPOOL_PATH"] = str(_SCRATCH / "spool")

from fastapi.testclient import TestClient  # noqa: E402  (env must precede the import)

import api_maquette  # noqa: E402
from draft_smoke import _attestation_lines, _write_pdf  # noqa: E402  (PII-free corpus)
from ocr_bifunction.drafting import (  # noqa: E402
    DraftingDocument,
    cluster_unknown_documents,
    draft_from_cluster,
)
from ocr_bifunction.reader import read_document  # noqa: E402
from ocr_bifunction.repository import Job, SqliteRepository  # noqa: E402
from ocr_bifunction.review_repository import (  # noqa: E402
    Review,
    SqliteReviewRepository,
    Suggestion,
)
from ocr_bifunction.template_repository import SqliteTemplateRepository  # noqa: E402

_ORGANISM_SIRET = "11122233344455"  # fictional
_OTHER_SIRET = "99988877766655"  # fictional
_ALLOWED_CODES = ["B1V", "B2V", "BC", "BR", "H0B0"]

_CORPUS = [
    ("FICTIF Alice", "12/03/2024", "12/03/2027", "H0B0 B1V", "DOSSIER-2024-0117"),
    ("EXEMPLE Bruno", "05/11/2023", "05/11/2026", "B1V BR", "DOSSIER-2023-0492"),
    ("SPECIMEN Chloe", "28/06/2024", "28/06/2027", "H0B0 BR", "DOSSIER-2024-0663"),
]
# A 4th same-layout attestation carrying an INVENTED code (Z9X) — the softening case.
_INVENTED_CODE_DOC = (
    "MODELE David",
    "15/01/2024",
    "15/01/2027",
    "Z9X H0B0",
    "DOSSIER-2024-0801",
)

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


def _write_attestation(path: Path, values: tuple) -> None:
    lines = _attestation_lines(*values)
    lines.append((72, 560, f"SIRET : {_ORGANISM_SIRET}"))
    _write_pdf(path, lines)


def _payload_for(path: Path) -> dict:
    return {
        "files": [
            {
                "filename": path.name,
                "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        ],
        "document_type": "attestation",
    }


def _draft_base_template(attestation_paths: list[Path]) -> tuple[dict, str]:
    """Draft the base template; return it + the codes field name (for the vocabulary
    rule). The issuer field is added by hand: a single-organism cluster has an
    INVARIANT SIRET, which the drafting gate rightly refuses as a field (constant =
    structure) — the issuer field only emerges from curation or multi-organism data."""
    documents = []
    for path in attestation_paths:
        result = read_document(path, None)
        documents.append(
            DraftingDocument(source=path.name, text=result.text, lines=result.lines)
        )
    cluster = cluster_unknown_documents(documents)[0]
    report = draft_from_cluster(cluster, "attestation", "attestation_severity_01")
    if report.template is None:
        raise RuntimeError(f"draft failed: {report.reasons}")
    codes_field_name = next(
        name
        for name, value in report.extractions_by_source[
            attestation_paths[0].name
        ].items()
        if value == "H0B0 B1V"
    )
    template = report.template
    template["fields"].append(
        {
            "name": "emetteur_siret",
            "pattern": r"SIRET\s*:\s*([0-9]{14})",
        }
    )
    return template, codes_field_name


def _upsert(template: dict) -> None:
    template_repository = SqliteTemplateRepository(os.environ["OCR_STORE_PATH"])
    template_repository.upsert(template, active=True)
    template_repository.close()


def run() -> int:
    corpus_directory = _SCRATCH / "corpus"
    corpus_directory.mkdir()
    attestation_paths: list[Path] = []
    for index, values in enumerate(_CORPUS):
        path = corpus_directory / f"attestation_{index + 1}.pdf"
        _write_attestation(path, values)
        attestation_paths.append(path)
    invented_code_path = corpus_directory / "attestation_code_invente.pdf"
    _write_attestation(invented_code_path, _INVENTED_CODE_DOC)

    base_template, codes_field_name = _draft_base_template(attestation_paths)
    issuer_rule = {
        "check": "issuer_registry",
        "field": "emetteur_siret",
        "severity": "reject",
    }
    template = {
        **base_template,
        "validation": {
            **base_template["validation"],
            "required": [*base_template["validation"]["required"], issuer_rule],
        },
    }
    _upsert(template)

    with TestClient(api_maquette.app) as client:
        # 1. HARDEN: registry lists another organism -> read issuer OUT -> rejected.
        client.put(
            f"/v1/issuer-registry/{_OTHER_SIRET}", json={"label": "Autre organisme"}
        )
        hardened = client.post(
            "/v1/documents:validate", json=_payload_for(attestation_paths[0])
        ).json()
        _check(
            "severity=reject hardens issuer_registry: unknown issuer -> REJECTED",
            hardened["status"] == "rejected"
            and any("issuer_registry" in reason for reason in hardened["reasons"]),
        )

        # 2. FAIL-LOUD INVINCIBLE: empty registry -> undetermined -> review anyway.
        client.delete(f"/v1/issuer-registry/{_OTHER_SIRET}")
        undetermined = client.post(
            "/v1/documents:validate", json=_payload_for(attestation_paths[1])
        ).json()
        _check(
            "empty registry stays needs_review DESPITE severity=reject (undetermined)",
            undetermined["status"] == "needs_review"
            and any(
                "no organism registry" in reason for reason in undetermined["reasons"]
            ),
        )

        # 3. Recognized issuer -> auto.
        client.put(
            f"/v1/issuer-registry/{_ORGANISM_SIRET}",
            json={"label": "Centre de formation specimen"},
        )
        recognized = client.post(
            "/v1/documents:validate", json=_payload_for(attestation_paths[2])
        ).json()
        _check(
            "recognized issuer -> validated/auto",
            recognized["status"] == "validated" and recognized["verdict"] == "auto",
        )

        # 4. SOFTEN: vocabulary rejects an invented code by default; severity=review
        # routes it to a human instead.
        vocabulary_rule = {
            "check": "vocabulary",
            "field": codes_field_name,
            "allowed": _ALLOWED_CODES,
        }
        _upsert(
            {
                **template,
                "validation": {
                    **template["validation"],
                    "required": [*template["validation"]["required"], vocabulary_rule],
                },
            }
        )
        control = client.post(
            "/v1/documents:validate", json=_payload_for(invented_code_path)
        ).json()
        _check(
            "control (no severity): invented code -> rejected (vocabulary default)",
            control["status"] == "rejected",
        )
        _upsert(
            {
                **template,
                "validation": {
                    **template["validation"],
                    "required": [
                        *template["validation"]["required"],
                        {**vocabulary_rule, "severity": "review"},
                    ],
                },
            }
        )
        softened = client.post(
            "/v1/documents:validate", json=_payload_for(invented_code_path)
        ).json()
        _check(
            "severity=review softens vocabulary: invented code -> needs_review",
            softened["status"] == "needs_review"
            and any(
                "vocabulary check failed" in reason for reason in softened["reasons"]
            ),
        )

        # 5. CONFIG TYPO: unknown severity surfaces as a review reason, even on a pass.
        _upsert(
            {
                **template,
                "validation": {
                    **template["validation"],
                    "required": [
                        *base_template["validation"]["required"],
                        {**issuer_rule, "severity": "hard"},
                    ],
                },
            }
        )
        typo = client.post(
            "/v1/documents:validate", json=_payload_for(attestation_paths[0])
        ).json()
        _check(
            "unknown severity value -> fail-loud review reason (config typo surfaces)",
            typo["status"] == "needs_review"
            and any("unknown severity" in reason for reason in typo["reasons"]),
        )

        # 6. PROMOTION: a ticked candidate may carry a severity; bad value -> 400.
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
            **base_template,
            "template_id": "draft_attestation_severity_01",
        }
        review_id = review_repository.open_review(
            Review(
                job_id=job_id,
                projection={"source": "attestation_1.pdf", "lane": "rag"},
                suggestion=Suggestion(
                    template_id="draft_attestation_severity_01",
                    category="attestation",
                    template=draft_for_promotion,
                ),
            )
        )
        review_repository.close()
        candidates = draft_for_promotion["validation"]["required"]
        bad_severity = client.post(
            f"/v1/suggestions/{review_id}/validate",
            json={"required": [{**candidates[0], "severity": "explode"}]},
        )
        _check(
            "promotion refuses an unknown severity value (400)",
            bad_severity.status_code == 400,
        )
        promoted = client.post(
            f"/v1/suggestions/{review_id}/validate",
            json={"required": [{**candidates[0], "severity": "reject"}]},
        ).json()
        template_repository = SqliteTemplateRepository(os.environ["OCR_STORE_PATH"])
        promoted_row = template_repository.get("draft_attestation_severity_01")
        template_repository.close()
        _check(
            "promotion writes the ticked severity into the D2 required block",
            promoted.get("promoted_template_id") == "draft_attestation_severity_01"
            and promoted_row["validation"]["required"]
            == [{**candidates[0], "severity": "reject"}],
        )

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT per-check severity knob: {'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
