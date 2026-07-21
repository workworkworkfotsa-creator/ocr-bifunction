"""D3 — the review + template-suggestion store (organic-growth loop), behind a ReviewRepository ABC.

Stage ⑤ REMONTER's human layer made real, plus the mechanism that grows the template library: a
doc that matched no template gets an SLM-proposed template (a candidate id from the closed list of
known templates + the anchors that motivate it); the human validates; a validated suggestion is
promoted to D2 (the template becomes active). Per the fabrique doctrine (skill handoff-it) the
CONTRACT that crosses to IT is the TABLE; the storage engine is a jettisonable adapter behind a
`ReviewRepository` ABC (IT swaps `SqliteReviewRepository` for an internal-DB implementation).

D3 is a SEPARATE domain from D1 (jobs): a different owner (the reviewer / review UI writes D3; the
worker writes D1) and a different lifecycle. D3 REFERENCES a job by `job_id` and does NOT duplicate
its record — the record's single source of truth stays in D1. `projection` is a human-facing VIEW
(resume/analyse), not a second source of truth.

How the suggestion loop "communicates": THE `suggestion_status` COLUMN IS THE SIGNAL, exactly like
D1's `status`. A suggestion waiting for the human is a row with suggestion_status='pending';
validating it flips to 'validated', which is what the promotion step (D3 -> D2) polls. One writer
per phase (the reviewer owns the decision; promotion owns the D2 write) — the "contrat de colonnes"
that avoids a Python<->UI race.

Internal target-DB shape (co-freeze with IT, NOT frozen here): explicit created_at/updated_at (the
target DB may lack DEFAULT CURRENT_TIMESTAMP), a portable engine/charset, and `job_id` a real FK to
ocr_jobs. SQLite is the proxy and does not enforce the FK — the target-DB DDL is the contract
artifact to write at handoff.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ocr_bifunction.store import Store

# The suggestion loop signal (mirrors D1's status column). A suggestion "waiting for the human"
# = pending; the human drives pending -> validated (promote to D2) | rejected (hallucinated).
SUGGESTION_PENDING = "pending"
SUGGESTION_VALIDATED = "validated"
SUGGESTION_REJECTED = "rejected"

# The human's verdict on the reviewed record itself (independent of any suggestion).
DECISION_ACCEPT = "accept"
DECISION_REJECT = "reject"

# The reserved id the SLM must return when a doc matches no known template (closed-list output).
UNKNOWN_TEMPLATE_ID = "UNKNOWN"


@dataclass
class Suggestion:
    """An SLM-proposed template for a doc that matched no existing template. The model PROPOSES a
    candidate id (from the closed list of known template ids) plus the anchors it saw; the
    deterministic layer re-verifies those anchors, and the human validates before the template is
    promoted to D2. `status` is the loop signal (pending -> validated | rejected). A `template_id`
    of None means the model answered UNKNOWN (nothing to try -> straight to the human)."""

    template_id: (
        str | None
    )  # a known template_id, or None when the model answered UNKNOWN
    category: str | None = None
    anchors: list[str] = field(
        default_factory=list
    )  # re-verifiable structural anchors (no PII)
    status: str = SUGGESTION_PENDING
    # The FULL draft template (drafting lane): the curated-content candidate travels
    # WITH the suggestion so the reviewer sees — and promotion activates — exactly what
    # the deterministic draft proved on its cluster. None for closed-list suggestions
    # (the SLM lane proposes a KNOWN template_id; the content lives in D2/files).
    template: dict | None = None


@dataclass
class Review:
    """One D3 row: the human review of a D1 job, optionally staging a template suggestion.

    It REFERENCES the job by `job_id` and never duplicates its record (the record's single source
    of truth stays in D1). `projection` is a view built FOR the human (source, lane, a short
    summary), explicitly not a second source of truth. `decision` is the human's accept/reject on
    the record; `suggestion` (when present) carries the organic-growth candidate.

    `field_corrections` is the human's EDIT of the extracted record: `{field: {"from": machine
    value, "to": human value}}`. It lives HERE, not in D1, for two reasons. The writer rule — the
    UI writes D3, the watchdog writes D1 — and the audit: keeping what the machine read next to
    what the human put in its place is what makes a correction reviewable later (and what tells
    a recurring OCR weakness from a one-off). The watchdog APPLIES it to D1 when the review is
    accepted; until then D1 still says, honestly, what the machine read."""

    job_id: int
    projection: dict[str, str | None] = field(default_factory=dict)
    comment: str | None = None
    decision: str | None = None  # accept | reject | None (not yet decided)
    suggestion: Suggestion | None = None
    field_corrections: dict[str, dict] = field(default_factory=dict)
    review_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ReviewRepository(ABC):
    """The D3 store seam. IT swaps the SQLite proxy for an internal-DB implementation; the review UI
    and the promotion step talk only through the rows this exposes."""

    @abstractmethod
    def open_review(self, review: Review) -> int:
        """Insert a review row for a D1 job; return its assigned review_id."""

    @abstractmethod
    def get(self, review_id: int) -> Review | None: ...

    @abstractmethod
    def by_job(self, job_id: int) -> Review | None:
        """The review opened for a given D1 job, if any (a job is reviewed at most once)."""

    @abstractmethod
    def pending_suggestions(self) -> list[Review]:
        """The rows whose suggestion is pending — the organic-growth queue the human works."""

    @abstractmethod
    def decided(self) -> list[Review]:
        """The rows where the human HAS decided (accept/reject) — what the worker's sweep
        reads to close the corresponding D1 jobs (the UI never writes D1.status itself)."""

    @abstractmethod
    def record_decision(
        self, review_id: int, *, comment: str | None = None, decision: str | None = None
    ) -> None:
        """The human's verdict on the reviewed record (comment and/or accept/reject)."""

    @abstractmethod
    def record_field_corrections(
        self, review_id: int, corrections: dict[str, dict]
    ) -> None:
        """Store the human's edits of the extracted record ({field: {"from", "to"}}).

        Replaces the whole map, so re-saving is idempotent and un-editing a field (setting it
        back to the machine value) removes it — the caller decides what counts as a change."""

    @abstractmethod
    def stage_suggestion(self, review_id: int, suggestion: Suggestion) -> None:
        """Attach a suggestion to an EXISTING review (a review is opened at intake; the SLM lane
        or the reviewer stages the candidate later — two writes, one row)."""

    @abstractmethod
    def set_suggestion_status(self, review_id: int, status: str) -> None:
        """Advance a suggestion (pending -> validated | rejected) — the loop transition."""

    @abstractmethod
    def close(self) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_reviews (
    review_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                  INTEGER NOT NULL,
    projection              TEXT,
    comment                 TEXT,
    decision                TEXT,
    suggested_template_id   TEXT,
    suggested_category      TEXT,
    suggested_anchors       TEXT,
    suggested_template_json TEXT,
    suggestion_status       TEXT,
    field_corrections       TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ocr_reviews_suggestion ON ocr_reviews (suggestion_status);
CREATE INDEX IF NOT EXISTS idx_ocr_reviews_job ON ocr_reviews (job_id);
"""

# Columns added after the first proxy shipped; existing local .sqlite files gain them on
# open. (The target-DB DDL at handoff bakes them in — this is proxy-only migration.)
_MIGRATION_COLUMNS = {
    "suggested_template_json": (
        "ALTER TABLE ocr_reviews ADD COLUMN suggested_template_json TEXT"
    ),
    "field_corrections": "ALTER TABLE ocr_reviews ADD COLUMN field_corrections TEXT",
}

_COLUMNS = (
    "review_id, job_id, projection, comment, decision, suggested_template_id, "
    "suggested_category, suggested_anchors, suggested_template_json, "
    "suggestion_status, field_corrections, created_at, updated_at"
)


class SqliteReviewRepository(ReviewRepository):
    """The jettisonable SQLite proxy of D3 — same table shape IT will build on the internal target DB.

    Timestamps are written EXPLICITLY (the target DB may lack DEFAULT CURRENT_TIMESTAMP);
    the clock is injectable for tests. The DB file may hold projected fields (PII) and is gitignored.
    """

    def __init__(self, store: Store | str | Path = "ocr_store.sqlite") -> None:
        self._store = store if isinstance(store, Store) else Store(store)
        self._connection = self._store.connection
        self._clock = self._store.clock
        self._store.ensure_schema(
            _SCHEMA, table="ocr_reviews", migrations=_MIGRATION_COLUMNS
        )

    def open_review(self, review: Review) -> int:
        now = self._clock()
        suggestion = review.suggestion
        cursor = self._connection.execute(
            "INSERT INTO ocr_reviews (job_id, projection, comment, decision, "
            "suggested_template_id, suggested_category, suggested_anchors, "
            "suggested_template_json, suggestion_status, field_corrections, "
            "created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review.job_id,
                json.dumps(review.projection, ensure_ascii=False),
                review.comment,
                review.decision,
                suggestion.template_id if suggestion else None,
                suggestion.category if suggestion else None,
                json.dumps(suggestion.anchors, ensure_ascii=False)
                if suggestion
                else None,
                json.dumps(suggestion.template, ensure_ascii=False)
                if suggestion and suggestion.template is not None
                else None,
                suggestion.status if suggestion else None,
                json.dumps(review.field_corrections, ensure_ascii=False),
                now,
                now,
            ),
        )
        self._connection.commit()
        review.review_id = int(cursor.lastrowid)
        return review.review_id

    def _row_to_review(self, row: sqlite3.Row) -> Review:
        suggestion: Suggestion | None = None
        if row["suggestion_status"] is not None:
            suggestion = Suggestion(
                template_id=row["suggested_template_id"],
                category=row["suggested_category"],
                anchors=json.loads(row["suggested_anchors"] or "[]"),
                status=row["suggestion_status"],
                template=json.loads(row["suggested_template_json"])
                if row["suggested_template_json"]
                else None,
            )
        return Review(
            review_id=row["review_id"],
            job_id=row["job_id"],
            projection=json.loads(row["projection"] or "{}"),
            comment=row["comment"],
            decision=row["decision"],
            suggestion=suggestion,
            field_corrections=json.loads(row["field_corrections"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, review_id: int) -> Review | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM ocr_reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
        return self._row_to_review(row) if row else None

    def by_job(self, job_id: int) -> Review | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM ocr_reviews WHERE job_id = ? ORDER BY review_id LIMIT 1",
            (job_id,),
        ).fetchone()
        return self._row_to_review(row) if row else None

    def pending_suggestions(self) -> list[Review]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM ocr_reviews WHERE suggestion_status = ? "
            "ORDER BY review_id",
            (SUGGESTION_PENDING,),
        ).fetchall()
        return [self._row_to_review(row) for row in rows]

    def decided(self) -> list[Review]:
        rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM ocr_reviews WHERE decision IS NOT NULL "
            "ORDER BY review_id"
        ).fetchall()
        return [self._row_to_review(row) for row in rows]

    def record_decision(
        self, review_id: int, *, comment: str | None = None, decision: str | None = None
    ) -> None:
        assignments = ["updated_at = ?"]
        parameters: list[object] = [self._clock()]
        if comment is not None:
            assignments.append("comment = ?")
            parameters.append(comment)
        if decision is not None:
            assignments.append("decision = ?")
            parameters.append(decision)
        parameters.append(review_id)
        self._connection.execute(
            f"UPDATE ocr_reviews SET {', '.join(assignments)} WHERE review_id = ?",
            parameters,
        )
        self._connection.commit()

    def record_field_corrections(
        self, review_id: int, corrections: dict[str, dict]
    ) -> None:
        self._connection.execute(
            "UPDATE ocr_reviews SET field_corrections = ?, updated_at = ? "
            "WHERE review_id = ?",
            (
                json.dumps(corrections, ensure_ascii=False),
                self._clock(),
                review_id,
            ),
        )
        self._connection.commit()

    def stage_suggestion(self, review_id: int, suggestion: Suggestion) -> None:
        self._connection.execute(
            "UPDATE ocr_reviews SET suggested_template_id = ?, suggested_category = ?, "
            "suggested_anchors = ?, suggested_template_json = ?, "
            "suggestion_status = ?, updated_at = ? WHERE review_id = ?",
            (
                suggestion.template_id,
                suggestion.category,
                json.dumps(suggestion.anchors, ensure_ascii=False),
                json.dumps(suggestion.template, ensure_ascii=False)
                if suggestion.template is not None
                else None,
                suggestion.status,
                self._clock(),
                review_id,
            ),
        )
        self._connection.commit()

    def set_suggestion_status(self, review_id: int, status: str) -> None:
        self._connection.execute(
            "UPDATE ocr_reviews SET suggestion_status = ?, updated_at = ? WHERE review_id = ?",
            (status, self._clock(), review_id),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
