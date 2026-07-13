"""The 3-state routing verdict — one home for auto/review/reject and its edge mappings.

Before this module the same reject > review > auto precedence was authored twice (the
structured lane in `template.ValidationOutcome`, the CI/MRZ lane in `reconcile`) and then
re-expressed through ~6 remap tables and ~4 if/elif ladders across the router, orchestrator,
API door and async worker — in two rival vocabularies ("human" vs "review"), with one sink
silently dropping `reject`. This value object is the single authority:

  - `from_reasons` is the ONE place the precedence lives; both lanes feed it reason-buckets.
  - `d1_status` / `wire_status` are the ONLY serializations of a verdict to a persisted status
    or an HTTP status — the single source of truth for the boundary vocabulary.

Canonical vocabulary (business glossary + `needs_review` status): auto / review / reject.
`Verdict` is the currency INSIDE the compute lanes; the DB column and the wire JSON stay
strings, serialized at the edge via `.value` / `.d1_status` / `.wire_status` (the `Job` DTO
is the internal-DB contract row — stringly typed there on purpose). `.value` doubles as the batch
`DocumentRecord.outcome` (auto/review/reject), so there is no separate outcome mapping.
"""

from __future__ import annotations

from enum import Enum

from ocr_bifunction.status import STATUS_DONE, STATUS_NEEDS_REVIEW, STATUS_REJECTED


class Verdict(Enum):
    """A document's routing verdict. AUTO = validated confidently; REVIEW = doubtful/unknown/
    pending -> the human queue; REJECT = PROVEN invalid (anti-fraud), auto-terminal, no human."""

    AUTO = "auto"
    REVIEW = "review"
    REJECT = "reject"

    @classmethod
    def from_reasons(
        cls, reject_reasons: list[str], review_reasons: list[str]
    ) -> "Verdict":
        """The ONE precedence: reject > review > auto. A single proven-invalid reason rejects;
        else any undetermined/pending reason routes to review; else the document auto-validates.
        Both lanes feed their classified reason-buckets here instead of re-deriving the ladder."""
        if reject_reasons:
            return cls.REJECT
        if review_reasons:
            return cls.REVIEW
        return cls.AUTO

    @property
    def d1_status(self) -> str:
        """The persisted D1 `status` column this verdict lands the row in (a TERMINAL state)."""
        return {
            Verdict.AUTO: STATUS_DONE,
            Verdict.REVIEW: STATUS_NEEDS_REVIEW,
            Verdict.REJECT: STATUS_REJECTED,
        }[self]

    @property
    def wire_status(self) -> str:
        """The upload-facing HTTP response `status` field for this verdict."""
        return {
            Verdict.AUTO: "validated",
            Verdict.REVIEW: "needs_review",
            Verdict.REJECT: "rejected",
        }[self]
