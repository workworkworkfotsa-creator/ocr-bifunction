"""D1 status vocabulary — the queue/coordination lifecycle states (a leaf, no deps).

This is CONTRACT vocabulary: the `status` column IT reads (cf. docs/contrat-bd-destination.md).
It lives in its own dependency-free module so both the persistence adapter (`repository.py`)
and the core `Verdict` value object (`verdict.py`) can import it WITHOUT a core->adapter
dependency, and importing `verdict` never transitively pulls in `sqlite3`.

The worker drives received -> processing -> a TERMINAL state:
  done         — validated (auto) or a human accepted it.
  needs_review — doubtful/unknown -> the human queue (the ⑤ pile).
  rejected     — PROVEN invalid (bad date maths, MRZ recto/verso mismatch, invented code):
                 the anti-fraud verdict is `reject`, auto-terminal, NO human review. Distinct
                 from `failed`, which is a PROCESSING failure (crash/poison-pill), not a
                 verdict on the document's validity.
  failed       — processing gave up (lease/attempts cap).
"""

from __future__ import annotations

STATUS_RECEIVED = "received"
STATUS_PROCESSING = "processing"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_DONE = "done"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
