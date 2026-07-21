# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version stays on
`0.y.z`, the intake contract (tables + config surfaces) is not yet frozen and a breaking change may
land in a MINOR bump; `1.0.0` will mark the contract co-frozen with the IT integration.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
`fix:` → PATCH, `feat:` → MINOR, `feat!:` or a `BREAKING CHANGE:` footer → MAJOR. `docs:`, `chore:`,
`refactor:`, `test:` do not trigger a release.
**While on `0.y.z`, that last mapping is SUSPENDED: `feat!:` / `BREAKING CHANGE:` bumps MINOR, not
MAJOR** (SemVer §4 — anything may change at any time under `0.y.z`). Breaking the intake contract is
expected while it is being designed, and must not force a premature `1.0.0`. **`1.0.0` is a
DECISION, not a threshold**: it is cut the day the contract (tables + config surfaces) is co-frozen
with the IT integration — never because breaking changes accumulated. Staying on small numbers is
deliberate.

Keep `[Unreleased]` up to date as changes land; at release time, rename it to the new version, add
the date, and bump `version` in `pyproject.toml` to match the tag.

## [Unreleased]

### Added
- **Field provenance (page + bbox)** — `extract_fields` no longer destroys geometry at the last
  point it still exists. It returns `ExtractedField(value, spans, origin)` on BOTH extraction paths:
  the geometry-anchor path carries the value line's box, and the regex path (born-digital invoices)
  recovers its span from the match's character range over the joined text. A value straddling lines,
  or pages, yields several spans — hence a list. This is what lets a reviewer be shown the region a
  value was read from, without which they can neither validate nor correct it. Provenance that does
  not exist stays empty (MRZ-backfilled ID-card fields, a VLM read whose synthetic boxes encode
  reading order rather than position, nothing matched) and is never fabricated.
- **Per-word narrowing of the highlighted region** — a born-digital PDF is read in PyMuPDF *blocks*,
  which are paragraph-sized (up to 30 % of a page), so highlighting the block told a reviewer
  "somewhere in here". The span now covers only the words the value occupies, selected BY CHARACTER
  POSITION — never by matching a word's spelling, which lands on whichever occurrence comes first:
  measured on a real invoice, spelling selection produced a box 3x LARGER than the block it was
  meant to shrink, because a date's words also appeared in the document title. Measured gain on that
  invoice: 3.0x to 7.7x smaller area. OCR backends expose no word grain and need none — their boxes
  are already line-sized — so they fall back to the whole line.
- **Character-integrity guard** — an intrinsic, model-agnostic check on extracted text
  (`text_integrity_guard`), computed at a single seam in `read_document` so every backend (PDF text
  layer, OCR engine, `.docx`, resilient converter) is covered, and applied to both router lanes. It
  separates *irreversible loss* (a `U+FFFD` already present — the bytes are gone, hard flag, no
  repair) from *repairable mojibake* (UTF-8 read as latin-1/cp1252 — reversed and explained, offered
  as a suggestion a human validates, never auto-applied). Corrupted characters can never
  auto-validate: a non-clean read escalates `auto` to `review`, while a `reject` is never softened.
  This closes the character side of the semantic edge — the side a second independent reader cannot
  corroborate, since every text-layer extractor trusts the same broken PDF `ToUnicode` CMap and so
  agrees on the same garbage.

### Changed
- **BREAKING (D1 payload)** — `ocr_jobs.record_fields` is now
  `{name: {value, origin, spans: [{page_index, bbox}]}}` instead of `{name: value}`, one shape for
  every lane. Read it through `template.payload_value`. The verdict engine is untouched: it consumes
  the value-only projection (`field_values`).
- **Spans are normalized to the page** — `bbox` is four fractions in `[0, 1]` of page width/height,
  not the reader's native units. Those differ by backend (PDF points at 72 dpi for a text layer,
  pixels of a 200 dpi render for OCR, ~2.78x apart) and the payload said neither which nor the page
  size, so a box was literally unplaceable. Normalized, a consumer draws it with no unit, no dpi and
  no dimensions to carry, and the value survives a change of render resolution or OCR engine.

### Removed
- **Automatic table corroboration** — comparing two independent table reconstructions by SHAPE
  (rows × columns) was implemented and proved 6/6 by its smoke, then invalidated by the first real
  run: it diverged on 100 % of documents. The two readers do not disagree about quality, they apply
  different *segmentation* conventions ("what counts as one table"), and a detector that always
  fires detects nothing. Truth is not derivable from two extractors that contradict each other, so
  the retained path is **human adjudication** (page image next to both reconstructions). The module,
  its smoke and its run harness were deleted rather than kept as dead code that must not be wired.
- **`markitdown` dependency** — added solely for the corroboration above; `pdfplumber`, which the
  adjudication harness actually calls, is now an explicit dependency instead of a transitive one.

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
