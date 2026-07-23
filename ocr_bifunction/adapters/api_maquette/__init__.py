"""API maquette — a thin network door over the proven CI submission pipeline.

This is a PEDAGOGICAL MOCK, not production. Its only job is to let the contract run so we
can *see* what "having an API" means: upload one CI submission (any mix of images and/or a
combined recto+verso PDF), get back a stable envelope. The value lives in
`process_ci_submission`; this file only exposes it behind an HTTP door. The pipeline is NOT
touched.

The upload-facing contract has four outcomes (the upload UI acts on `status`):
  - validated   (200) — both sides received and confident;
  - pending     (202) — both sides received but doubtful: escalated async, poll job_id;
  - incomplete  (200) — one side missing: `missing` says which to ask the user for;
  - unrecognized(200) — not a CI submission at all.

Two lanes, one DOOR — the API never processes async work itself:
  - FAST PATH (in the request) — process_ci_submission with NO escalation engine; EVERY
    outcome leaves a D1 row (done/auto, needs_review, …) so D1 is the single source (④).
  - ESCALATION (off the request path) — a complete-but-doubtful (`review`) submission is
    SPOOLED to disk (`spool/<sub>/`, the row's `document_ref`) and written as a D1 row
    `status='received'`, answered `202 pending`. The row IS the queue entry: the SEPARATE
    watchdog worker process (worker_watchdog.py) claims it, re-runs WITH the VLM, and flips
    it to a terminal state. Restart-safe: the table survives, an in-memory queue would not.

The D1 `Repository` (SqliteRepository proxy) and the spool directory are the disposable
adapters of destination "domain 1 — jobs + queue": this API, the batch orchestrator and the
watchdog all write/read the SAME `ocr_jobs` table (repository.py) — one column contract,
several producers/consumers, one writer per phase. IT swaps store, hosting, auth, TLS.

Run it:
    uv run uvicorn ocr_bifunction.adapters.api_maquette:app --reload          # the door
    uv run python -m ocr_bifunction.adapters.worker_watchdog                  # the worker (separate process)
"""

from fastapi import FastAPI

from ocr_bifunction.adapters.api_maquette import (
    door,
    governance_routes,
    pages,
    review_routes,
)

app = FastAPI(
    title="OCR BiFunction — API maquette",
    version="1",
    description="Thin mock door over the CI submission pipeline. Not production.",
)

# Included in the order the endpoints were declared when this was one file. Every
# path is distinct, so the order carries no matching precedence — it is kept so the
# generated OpenAPI page reads the same way it always did.
app.include_router(door.router)
app.include_router(pages.router)
app.include_router(review_routes.router)
app.include_router(governance_routes.router)

__all__ = ["app"]
