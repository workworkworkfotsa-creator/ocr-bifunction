"""D1 — the jobs + record store (async coordination hub), behind a Repository ABC.

Stage ④ CENTRALISER made real, and the mechanism the two halves use to coordinate async work.
Per the fabrique doctrine (skill handoff-it) the CONTRACT that crosses to IT is the TABLE; the
storage engine is a jettisonable adapter behind a `Repository` ABC (IT swaps `SqliteRepository`
for a `MariaDbRepository`). Nothing calls across the halves directly — they talk THROUGH the
table.

How the tables "communicate" that a record is waiting for async: THE `status` COLUMN IS THE
SIGNAL. A document waiting for the escalation worker is a row with `status='received'` and
`execution_lane='escalation'`; the worker POLLS that (`pending('received', 'escalation')`),
flips it to `processing`, does the work, writes the record back, flips to `done`/`needs_review`.
No message bus. Safety = one writer per phase (the worker owns `status`; a review UI only READS
it) — the "contrat de colonnes" that avoids a Python↔PHP race.

Template SUGGESTION (D3) will follow the SAME status-driven, human-validated loop, keyed to D1
by `job_id` — a different job type, one mechanism. Not built here: today only D1, per
`docs/contrat-bd-destination.md`.

MariaDB target (co-freeze with IT, NOT frozen here): explicit `created_at`/`updated_at`
(MariaDB 5.5 has no `DEFAULT CURRENT_TIMESTAMP`), utf8, InnoDB. SQLite is the proxy and does
not enforce these — the MariaDB DDL is the contract artifact to write at handoff.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# The async-coordination states. A doc "waiting for async" = received/escalation; the worker
# drives received -> processing -> a TERMINAL state:
#   done         — validated (auto) or a human accepted it.
#   needs_review — doubtful/unknown -> the human queue (the ⑤ pile).
#   rejected     — PROVEN invalid (bad date maths, MRZ recto/verso mismatch, invented code):
#                  the anti-fraud verdict is `reject`, auto-terminal, NO human review. Distinct
#                  from `failed`, which is a PROCESSING failure (crash/poison-pill), not a
#                  verdict on the document's validity.
#   failed       — processing gave up (lease/attempts cap).
STATUS_RECEIVED = "received"
STATUS_PROCESSING = "processing"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_DONE = "done"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"


@dataclass
class Job:
    """One D1 row: a document's consolidated record PLUS its coordination status.

    The record (`record_fields`) is the SINGLE SOURCE OF TRUTH — it lives here, not duplicated
    elsewhere. `status` is the async signal; `execution_lane` says whether it went the fast
    path or needs escalation; `verdict` is the ④/⑤ auto/human decision."""

    source: str
    category_lane: str  # 'ci' | 'structured' | 'rag'
    status: str  # one of STATUS_*
    execution_lane: str = "fast"  # 'fast' | 'escalation'
    verdict: str | None = None  # 'auto' | 'human'
    category: str | None = None
    template_id: str | None = None
    record_fields: dict[str, str | None] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    request_id: str | None = None  # idempotence key (API); None for batch
    document_ref: str | None = (
        None  # storage pointer (spool dir) for async work on the bytes
    )
    attempts: int = 0  # times a worker claimed this row (poison-pill cap)
    job_id: int | None = None  # assigned by the store on save
    created_at: str | None = None
    updated_at: str | None = None


class Repository(ABC):
    """The D1 store seam. IT swaps the SQLite proxy for a MariaDB implementation; both halves
    (Python worker, review UI) talk only through the rows this exposes."""

    @abstractmethod
    def save(self, job: Job) -> int:
        """Insert a job row; return its assigned job_id."""

    @abstractmethod
    def get(self, job_id: int) -> Job | None: ...

    @abstractmethod
    def pending(self, status: str, execution_lane: str | None = None) -> list[Job]:
        """The rows in `status` (optionally scoped to an execution_lane) — the queue query."""

    @abstractmethod
    def claim_next(self, execution_lane: str | None = None) -> Job | None:
        """Atomically claim the OLDEST `received` row: flip it to `processing` (+1 attempt) and
        return it, or None when the queue is empty. The claim is the portable two-step (SELECT
        candidate, then UPDATE ... WHERE status='received' checking rowcount) so two workers can
        NEVER process the same row — the same pattern IT reproduces in MariaDB."""

    @abstractmethod
    def recover_stale(self, lease_seconds: float, max_attempts: int) -> tuple[int, int]:
        """Re-queue `processing` rows whose lease expired (a worker crashed mid-job): back to
        `received` for a retry, or `failed` once attempts reached max (poison-pill cap).
        Returns (requeued_count, failed_count)."""

    @abstractmethod
    def update_status(
        self,
        job_id: int,
        status: str,
        *,
        verdict: str | None = None,
        record_fields: dict[str, str | None] | None = None,
        reasons: list[str] | None = None,
    ) -> None:
        """The worker's state transition: advance a job and (optionally) write its result."""

    @abstractmethod
    def close(self) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_jobs (
    job_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     TEXT,
    source         TEXT NOT NULL,
    category_lane  TEXT NOT NULL,
    category       TEXT,
    template_id    TEXT,
    status         TEXT NOT NULL,
    execution_lane TEXT NOT NULL,
    verdict        TEXT,
    record_fields  TEXT,
    reasons        TEXT,
    document_ref   TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ocr_jobs_status ON ocr_jobs (status, execution_lane);
"""

# Columns added after the first proxy shipped; existing local .sqlite files gain them on open.
# (The MariaDB DDL at handoff bakes them in — this is proxy-only migration.)
_MIGRATION_COLUMNS = {
    "document_ref": "ALTER TABLE ocr_jobs ADD COLUMN document_ref TEXT",
    "attempts": "ALTER TABLE ocr_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
}

_COLUMNS = (
    "job_id, request_id, source, category_lane, category, template_id, status, "
    "execution_lane, verdict, record_fields, reasons, document_ref, attempts, "
    "created_at, updated_at"
)


class SqliteRepository(Repository):
    """The jettisonable SQLite proxy of D1 — same table shape IT will build in MariaDB.

    Timestamps are written EXPLICITLY (mirroring MariaDB 5.5's lack of DEFAULT
    CURRENT_TIMESTAMP); the clock is injectable for tests. The DB file holds extracted fields
    (PII) and is gitignored."""

    def __init__(
        self,
        database_path: str | Path = "ocr_store.sqlite",
        *,
        clock: Callable[[], str] | None = None,
        check_same_thread: bool = True,
    ) -> None:
        self._clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        # check_same_thread=False lets the async escalation worker and the request threads
        # share ONE connection — D1's documented role (a worker writes `status`, readers poll
        # it). The caller must then serialize access with a lock (the API does), which covers
        # the dropped per-thread guard. Batch stays single-threaded, so the default is True.
        self._connection = sqlite3.connect(
            str(database_path), check_same_thread=check_same_thread
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        existing_columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(ocr_jobs)")
        }
        for column_name, alter_statement in _MIGRATION_COLUMNS.items():
            if column_name not in existing_columns:
                self._connection.execute(alter_statement)
        self._connection.commit()

    def save(self, job: Job) -> int:
        now = self._clock()
        cursor = self._connection.execute(
            "INSERT INTO ocr_jobs (request_id, source, category_lane, category, "
            "template_id, status, execution_lane, verdict, record_fields, reasons, "
            "document_ref, attempts, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.request_id,
                job.source,
                job.category_lane,
                job.category,
                job.template_id,
                job.status,
                job.execution_lane,
                job.verdict,
                json.dumps(job.record_fields, ensure_ascii=False),
                json.dumps(job.reasons, ensure_ascii=False),
                job.document_ref,
                job.attempts,
                now,
                now,
            ),
        )
        self._connection.commit()
        job.job_id = int(cursor.lastrowid)
        return job.job_id

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            request_id=row["request_id"],
            source=row["source"],
            category_lane=row["category_lane"],
            category=row["category"],
            template_id=row["template_id"],
            status=row["status"],
            execution_lane=row["execution_lane"],
            verdict=row["verdict"],
            record_fields=json.loads(row["record_fields"] or "{}"),
            reasons=json.loads(row["reasons"] or "[]"),
            document_ref=row["document_ref"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, job_id: int) -> Job | None:
        row = self._connection.execute(
            f"SELECT {_COLUMNS} FROM ocr_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._row_to_job(row) if row else None

    def pending(self, status: str, execution_lane: str | None = None) -> list[Job]:
        query = f"SELECT {_COLUMNS} FROM ocr_jobs WHERE status = ?"
        parameters: list[str] = [status]
        if execution_lane is not None:
            query += " AND execution_lane = ?"
            parameters.append(execution_lane)
        query += " ORDER BY job_id"
        rows = self._connection.execute(query, parameters).fetchall()
        return [self._row_to_job(row) for row in rows]

    def claim_next(self, execution_lane: str | None = None) -> Job | None:
        # Portable two-step claim (works verbatim on MariaDB 5.5): pick the oldest candidate,
        # then flip it ONLY IF still 'received' — rowcount 0 means another worker won the race,
        # so try the next candidate. With the PID-locked single watchdog this never loops; the
        # guard is belt-and-braces for the day two crons overlap.
        while True:
            candidate = self._connection.execute(
                f"SELECT {_COLUMNS} FROM ocr_jobs WHERE status = ?"
                + (" AND execution_lane = ?" if execution_lane is not None else "")
                + " ORDER BY job_id LIMIT 1",
                (STATUS_RECEIVED, execution_lane)
                if execution_lane is not None
                else (STATUS_RECEIVED,),
            ).fetchone()
            if candidate is None:
                return None
            cursor = self._connection.execute(
                "UPDATE ocr_jobs SET status = ?, attempts = attempts + 1, "
                "updated_at = ? WHERE job_id = ? AND status = ?",
                (
                    STATUS_PROCESSING,
                    self._clock(),
                    candidate["job_id"],
                    STATUS_RECEIVED,
                ),
            )
            self._connection.commit()
            if cursor.rowcount == 1:
                return self.get(candidate["job_id"])

    def recover_stale(self, lease_seconds: float, max_attempts: int) -> tuple[int, int]:
        now = datetime.fromisoformat(self._clock())
        cutoff = (now - timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        stale_rows = self._connection.execute(
            f"SELECT {_COLUMNS} FROM ocr_jobs WHERE status = ? AND updated_at < ?",
            (STATUS_PROCESSING, cutoff),
        ).fetchall()
        requeued_count, failed_count = 0, 0
        for row in stale_rows:
            job = self._row_to_job(row)
            if job.attempts >= max_attempts:
                self.update_status(
                    job.job_id,
                    STATUS_FAILED,
                    reasons=[
                        *job.reasons,
                        f"stale lease after {job.attempts} attempt(s) — gave up",
                    ],
                )
                failed_count += 1
            else:
                self.update_status(job.job_id, STATUS_RECEIVED)
                requeued_count += 1
        return requeued_count, failed_count

    def update_status(
        self,
        job_id: int,
        status: str,
        *,
        verdict: str | None = None,
        record_fields: dict[str, str | None] | None = None,
        reasons: list[str] | None = None,
    ) -> None:
        assignments = ["status = ?", "updated_at = ?"]
        parameters: list[object] = [status, self._clock()]
        if verdict is not None:
            assignments.append("verdict = ?")
            parameters.append(verdict)
        if record_fields is not None:
            assignments.append("record_fields = ?")
            parameters.append(json.dumps(record_fields, ensure_ascii=False))
        if reasons is not None:
            assignments.append("reasons = ?")
            parameters.append(json.dumps(reasons, ensure_ascii=False))
        parameters.append(job_id)
        self._connection.execute(
            f"UPDATE ocr_jobs SET {', '.join(assignments)} WHERE job_id = ?", parameters
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
