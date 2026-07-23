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

### Changed
- **The HTTP door becomes a package, one module per surface.** `api_maquette.py` was 1597 lines
  holding the contract, the stores, the intake lane, the review surface, five config surfaces and
  the HTML pages. It is now `adapters/api_maquette/` split along the banners the file already
  carried: `settings` (every path, first stop for an integrator), `contract` (the request/response
  envelopes — what another stack reimplements), `store_access` (one singleton per surface behind one
  lock — the only module a real database rewrites), `spool`, `door`, `pages`, `review_routes`,
  `governance_routes`. Every body was lifted verbatim by line range and no import was written by
  hand: each module got the original import block and the linter pruned it. Behaviour is unchanged
  and proven so — the 35-route table diffs empty on paths, methods and names, and the whole proof
  suite replays identically, including the fourteen harnesses that drive the door end to end.
  The package exports only `app`; anything else a proof touches now names its real module, because
  a re-export would have hidden the trap below.
- **`load_smoke` patches `api_maquette.door._handle_validated_document`**, not the package. The probe
  is installed by rebinding a module global, which only works when the endpoint resolves that name in
  its own module — moving the endpoint without repointing the probe would have left it silently
  uncalled. The existing `1 <= probe.peak` lower bound is what proves the patch is live.
- **The identity key moves out of `reconcile` into `ocr_bifunction/identity_key.py`.** It was
  imported by `validation/checks.py`, which put a package cycle between two concerns for one string
  function. It belongs to neither: reconcile compares a recto against its MRZ, the checks compare a
  name against a reference or a registry, and both need the SAME key or the two answers disagree on
  what "same person" means. Body unchanged; it now carries its own warning that folding accents does
  not loosen the match — real fraud is the close-name sibling, so tolerance there is a security
  trade-off, never free.
- **The flat layout becomes a tree by concern.** 38 modules in one folder and 60 scripts at the repo
  root had stopped being navigable. `ocr_bifunction/` is now `reading/` (+ `engines/`),
  `extraction/`, `validation/`, `flow/`, `knowledge/`, `storage/`, `governance/`, `adapters/`;
  `llama_transport.py` and `paths.py` stay at the package root because they are cross-cutting — not
  belonging to a concern folder is the statement. The 60 root scripts move to `proofs/`, kept in ONE
  flat folder because 47 of them import each other. One rename: `validation.py` →
  `validation/checks.py`, which is what it is. Behaviour is unchanged, and proven so: the whole
  proof suite replays with identical exit codes, and `ci_geometry_fingerprint` diffs empty against a
  worktree of the pre-move commit running the old code on the real CI.
- **Entry points move to `ocr_bifunction/adapters/`** — disposable by doctrine, but importable.
  `uv run uvicorn ocr_bifunction.adapters.api_maquette:app`,
  `uv run python -m ocr_bifunction.adapters.worker_watchdog`. This is what lets the proofs import
  the API without a single `sys.path` insert.

### Added
- **`ocr_bifunction/paths.py`** — the repo directories derived ONCE instead of in 28 files. Each of
  those did `Path(__file__).parent`, correct only while every one of them sat at the repo root; a
  missing `templates/` does not raise, it loads zero templates and extraction quietly returns
  nothing. The module fails LOUD if its root holds no `pyproject.toml`, so that class of silent
  failure cannot come back.
- **The project is now installable (editable)** via a `hatchling` build backend, so
  `import ocr_bifunction` resolves from any folder while the source stays in the repo.

## [0.3.0] - 2026-07-21

Two SILENT reading failures closed — a read that dropped most of a document while reporting
success, and geometry rules that quietly tightened on a better scan — plus the reviewer gaining
the ability to correct what was read, not only to judge it.

### Added
- **Editable extraction in review** — a reviewer can now correct an extracted value, not just judge
  it. The edit is staged in D3 as `{field: {"from": what the machine read, "to": what the human
  put}}` and applied to the D1 record by the watchdog when the review is accepted: the UI writes D3,
  the watchdog writes D1, one writer per column. Keeping both sides is what makes a correction
  auditable later — and what will tell a recurring OCR weakness from a one-off. A corrected field
  carries `origin: "human"` and NO span: a typed value sits nowhere on the page, and pointing at the
  box the machine read would show a region that no longer holds what the field says. Corrections are
  surgical (untouched fields keep value, origin and spans), a rejected review applies nothing, and
  re-submitting the machine's own value is not a correction.

### Fixed
- **The geometry rules silently depended on the capture resolution.** "Same column" and "same row"
  were absolute pixel constants tuned on ~1100 px card scans, so the same card scanned at 2200 px
  doubled every offset while the tolerance stayed put: the rule tightened until it stopped matching,
  and a BETTER scan extracted fewer fields. They are now expressed in LINE HEIGHTS — the document's
  own text scale — because "same column" is a typographic property, not a property of the sensor.
  The scale is the document's MEDIAN line height, not the anchor line's own: a born-digital "line"
  is a PyMuPDF block that can hold a whole paragraph, and sizing a tolerance on one would blow it
  up. Page fractions were tried first and rejected on measurement: across two real captures they
  varied 9.8 % against 2.9 % for line heights, and would have changed born-digital behaviour.
  Extraction on a real ID card is byte-identical (`ci_geometry_fingerprint.py`).
- **Pages whose content is in their images were read blind.** A PDF page went to the text layer as
  soon as it held 10 native characters, so a full-page photo with a caption cleared the bar, its
  images were never read, and the read reported success — a real 24-page photo book came out as
  6 218 characters of captions with no error and `needs_ocr` False. Such a page is now OCR'd IN
  ADDITION to its text layer: the exact captions are kept AND the image content is added. On that
  document the read goes from 6 218 to 20 113 characters. The criterion (images covering > 80 % of
  the page AND under 600 native characters) was calibrated on the whole corpus BEFORE being wired,
  and the conjunction is what makes it precise: coverage alone flagged 32 pages, 7 of which carry a
  real text layer under a full-page background image and must not be touched. With no OCR engine
  wired, such a page sets `needs_ocr` — the gap is declared rather than dropped. OCR boxes are
  rescaled into the page's points on those pages, so the template geometry rules never compare
  render pixels against PDF points on one page.

## [0.2.0] - 2026-07-21

Provenance: an extracted value can be located in the source document, so a reviewer can be shown
the region it was read from instead of being asked to trust it.

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
- **The review page shows WHERE a value came from** — clicking an extracted field draws its region
  over the document. Pages are now rendered server-side to PNG (`GET /v1/jobs/{id}/page`), because
  the previous `<embed>` handed PDFs to the browser's built-in viewer: an opaque plugin nothing can
  be drawn over, whose displayed page cannot even be chosen — and PDFs are exactly where provenance
  exists. Every document type is an `<img>` now, so the overlay is uniform and "go to page 12"
  becomes possible. The zone is placed in plain percentages: spans are already page fractions, so
  no unit, dpi or page size travels to the browser. An out-of-range page is a 404 rather than a
  silent fallback to page 0 — a wrong page under a highlight looks right while pointing elsewhere.
  Known limit, surfaced in the UI: a span names the page, not which file of a multi-file submission,
  so zones are drawn on the first retained file.

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

[Unreleased]: https://github.com/workworkworkfotsa-creator/ocr-bifunction/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/workworkworkfotsa-creator/ocr-bifunction/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/workworkworkfotsa-creator/ocr-bifunction/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/workworkworkfotsa-creator/ocr-bifunction/releases/tag/v0.1.0
