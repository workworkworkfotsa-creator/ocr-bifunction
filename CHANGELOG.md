# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version stays on
`0.y.z`, the intake contract (tables + config surfaces) is not yet frozen and a breaking change may
land in a MINOR bump; `1.0.0` will mark the contract co-frozen with the IT integration.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
`fix:` → PATCH, `feat:` → MINOR, `feat!:` or a `BREAKING CHANGE:` footer → MAJOR. `docs:`, `chore:`,
`refactor:`, `test:` do not trigger a release. Keep `[Unreleased]` up to date as changes land; at
release time, rename it to the new version, add the date, and bump `version` in `pyproject.toml` to
match the tag.

## [Unreleased]

## [0.1.0] - 2026-07-20

Initial baseline — the bi-mode document intake proven end-to-end on real documents.

### Added
- **Reading (stage ①)** — type-routed text extraction: PyMuPDF text layer for born-digital PDFs,
  RapidOCR for images and scanned pages, python-docx for `.docx`. Jettisonable `OcrEngine` slot with
  RapidOCR (real-time lane) and Docling / LightOnOCR-2 (batch and escalation lanes).
- **Categorise + validate (stages ②③)** — JSON extraction templates; MRZ parsing (ICAO TD1 and
  legacy, four check digits); ID-card recto/verso reconciliation; born-digital invoice extraction;
  a three-state verdict (auto / review / reject) driven by a config-driven check registry with
  per-check severity.
- **Bi-mode flow** — a single `intake.handle_document` processing layer traversed by both regimes;
  a real-time API door and an asynchronous worker; three execution modes; a human review surface;
  nightly template drafting and promotion; SLM-assisted template suggestion.
- **Governance and config surfaces** — non-conformity policies, capacity levers, and `use_case` API
  keys, each with its own editable surface.
- **Conversion resilience** — a completeness/error guard (`conversion_guard`) and resilient
  multi-page conversion (split into page-range batches, retry dropped pages under a decreasing
  batch-size schedule, reconcile by absolute page number), proven on real Docling.

[Unreleased]: https://github.com/workworkworkfotsa-creator/ocr-bifunction/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/workworkworkfotsa-creator/ocr-bifunction/releases/tag/v0.1.0
