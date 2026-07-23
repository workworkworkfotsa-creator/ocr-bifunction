"""Where the door's data lives, and its two display knobs.

First stop for an integrator: every path this adapter touches is declared here, and
each one is a POC proxy (`templates/` stands in for a table, `spool/` for object
storage). Both `STORE_PATH` and `SPOOL_ROOT` are env-overridable, which is what lets
the proofs run against a scratch store without touching the real one."""

from __future__ import annotations

import os
from pathlib import Path


from ocr_bifunction.paths import PROJECT_ROOT


TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"
# Default suffix when an uploaded filename carries none (a CI photo is usually one format).
DEFAULT_SUFFIX = ".jpg"

# Where doubtful submissions' bytes wait for the watchdog (PII on disk, gitignored; the
# worker purges each job's directory on terminal state). Env-overridable like the store.
SPOOL_ROOT = Path(os.environ.get("OCR_SPOOL_PATH", "spool"))
STORE_PATH = os.environ.get("OCR_STORE_PATH", "ocr_store.sqlite")
# Display resolution for the rendered page preview. ANY value works: provenance spans are
# normalized to the page, so the overlay is dpi-independent — precisely what normalizing bought.
PAGE_RENDER_DPI = 150
UI_DIRECTORY = PROJECT_ROOT / "ui"
