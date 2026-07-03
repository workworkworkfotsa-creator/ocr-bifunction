"""D-c (part 1) live proof — the constrained SLM names a deterministic draft's fields.

Reuses draft_smoke's synthetic, PII-free born-digital corpus (no OCR): cluster the
attestations, draft deterministically (D-b), then wake the SLM to NAME the placeholder
fields (D-c) and prove the deterministic disposal:
  1. every placeholder is mapped to a safe, unique identifier;
  2. the renamed draft still matches + extracts + validates on the whole cluster
     (the re-test gate, unchanged);
  3. the field VALUES are unchanged by the rename (a pure relabel).

Needs the shared llama-swap up (granite). Run from the project root:
    uv run python field_naming_check.py
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from draft_smoke import _build_corpus
from ocr_bifunction.drafting import (
    DraftingDocument,
    cluster_unknown_documents,
    draft_from_cluster,
)
from ocr_bifunction.field_naming import name_draft_fields
from ocr_bifunction.reader import read_document
from ocr_bifunction.template import extract_fields, match_template

_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

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
        documents = [
            DraftingDocument(
                source=path.name,
                text=(result := read_document(path)).text,
                lines=result.lines,
            )
            for path in corpus_paths
        ]
        clusters = cluster_unknown_documents(documents)
        attestation_cluster = next(
            cluster
            for cluster in clusters
            if cluster[0].source.startswith("attestation")
        )

        print("=== D-b deterministic draft ===")
        report = draft_from_cluster(
            attestation_cluster, "attestation", "draft_attestation_01"
        )
        assert report.template is not None, "D-b draft should be accepted"
        draft = report.template
        placeholder_names = [field_entry["name"] for field_entry in draft["fields"]]
        print(f"  placeholder fields: {placeholder_names}")

        print("\n=== D-c constrained naming (live SLM) ===")
        named = name_draft_fields(draft, attestation_cluster)
        print(f"  reasons: {named.reasons or '(none)'}")
        for placeholder, final_name in named.name_mapping.items():
            arrow = "==" if placeholder == final_name else "->"
            print(f"    {placeholder:24s} {arrow} {final_name}")

        _check(
            "renamed draft accepted (re-test green, no fallback)",
            not named.reasons,
            f"(reasons={named.reasons})",
        )
        final_names = [field_entry["name"] for field_entry in named.template["fields"]]
        _check(
            "every placeholder mapped, one-to-one",
            set(named.name_mapping) == set(placeholder_names)
            and len(set(final_names)) == len(final_names),
            f"(mapping={named.name_mapping})",
        )
        _check(
            "all final names are safe identifiers",
            all(_IDENTIFIER_PATTERN.match(name) for name in final_names),
            f"(names={final_names})",
        )
        _check(
            "validation rules follow the rename (no dangling field)",
            {rule["field"] for rule in named.template["validation"]["required"]}
            == set(final_names),
        )

        print("\n=== re-test on the whole cluster (renamed draft) ===")
        for document in attestation_cluster:
            matched = match_template(document.lines, [named.template]) is not None
            extracted = extract_fields(document.lines, named.template)
            filled = sum(1 for value in extracted.values() if value)
            print(
                f"  {document.source}: match={matched} fields_filled={filled}"
                f"/{len(final_names)}"
            )
            _check(
                f"{document.source}: matches and fills every renamed field",
                matched and filled == len(final_names),
            )

        # A rename is a pure relabel: the VALUES extracted under the new names must equal
        # the values the deterministic draft extracted under the placeholders.
        print("\n=== value invariance (relabel changes names, not values) ===")
        for document in attestation_cluster:
            before = extract_fields(document.lines, draft)
            after = extract_fields(document.lines, named.template)
            remapped_before = {
                named.name_mapping[name]: value for name, value in before.items()
            }
            _check(
                f"{document.source}: values unchanged by the rename",
                remapped_before == after,
                f"(before={remapped_before}, after={after})",
            )

        print(f"\nD-c NAMING PROOF PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
