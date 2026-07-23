"""Execution policy — the config surface that decides WHEN a submission is processed.

The bi-mode (CLAUDE.md) gives the pipeline two regimes — in-the-request (seconds) and
behind-the-request (batch/night). Until now the choice was HARDCODED in the API door
(`document_type == "carte_identite"` -> CI flow, everything else -> sync single-doc).
Infrastructure and needs change, so the mapping category -> regime must be an OPERATED
surface, not code: this module is that surface.

Three execution modes, one resolution rule:
  - sync            — processed inside the HTTP request (seconds; RapidOCR-class engines).
  - async_immediate — spooled + queued, drained by the CONTINUOUSLY running watchdog
                      (minutes; execution_lane 'deferred').
  - async_nightly   — spooled + queued, drained only by the nightly pass
                      (`worker_watchdog.py --nightly`, scheduler parity; execution_lane 'nightly').

Resolution: the policy row for the document's category wins; missing category falls back
to the '*' default row. The API caller may send an optional `processing_mode` hint — it is
honored ONLY where the policy says `override_allowed` (cohabitation: e.g. carte_identite
stays locked sync while factures accept a nightly push). Everything is traced in `reasons`.

Fabrique doctrine (skill handoff-it, "leviers" pattern): defaults live IN CODE
(`DEFAULT_EXECUTION_POLICIES`), the DB table (`ocr_execution_policies`) is the override
store edited through the /policies UI without redeploy, and `seed_defaults` only inserts
MISSING rows so operator edits survive restarts. The SQLite store is the jettisonable
proxy of the internal target-DB table (explicit timestamps, portable shape).
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ocr_bifunction.storage.store import Store

EXECUTION_MODE_SYNC = "sync"
EXECUTION_MODE_ASYNC_IMMEDIATE = "async_immediate"
EXECUTION_MODE_ASYNC_NIGHTLY = "async_nightly"
EXECUTION_MODES = (
    EXECUTION_MODE_SYNC,
    EXECUTION_MODE_ASYNC_IMMEDIATE,
    EXECUTION_MODE_ASYNC_NIGHTLY,
)

# The fallback policy row: applies to any category without its own row.
DEFAULT_POLICY_CATEGORY = "*"

# Which D1 execution_lane an async mode enqueues into. The continuously running watchdog
# drains 'escalation' + 'deferred'; only a `--nightly` pass also drains 'nightly'.
EXECUTION_LANE_FOR_ASYNC_MODE = {
    EXECUTION_MODE_ASYNC_IMMEDIATE: "deferred",
    EXECUTION_MODE_ASYNC_NIGHTLY: "nightly",
}


@dataclass
class ExecutionPolicy:
    """One row of the surface: a category's execution mode + whether callers may override."""

    category: str  # a document category, or '*' for the default row
    execution_mode: str  # one of EXECUTION_MODES
    override_allowed: bool = False  # may the API caller's processing_mode hint win?
    created_at: str | None = None
    updated_at: str | None = None


# In-code defaults (the seed). carte_identite is LOCKED sync: the CI flow is the realtime
# lane by design, and its doubtful cases already escalate through their own path — a caller
# hint must not bypass that. Everything else defaults to sync but accepts a caller push.
DEFAULT_EXECUTION_POLICIES = (
    ExecutionPolicy(
        category=DEFAULT_POLICY_CATEGORY,
        execution_mode=EXECUTION_MODE_SYNC,
        override_allowed=True,
    ),
    ExecutionPolicy(
        category="carte_identite",
        execution_mode=EXECUTION_MODE_SYNC,
        override_allowed=False,
    ),
)


@dataclass
class ResolvedExecution:
    """The door's decision for one submission, with its audit trail."""

    execution_mode: str
    policy_category: str  # which row decided ('*' when the category had no own row)
    reasons: list[str] = field(default_factory=list)


def resolve_execution(
    category: str | None,
    requested_mode: str | None,
    policies: dict[str, ExecutionPolicy],
) -> ResolvedExecution:
    """Pure resolution: category policy (fallback '*'), then the caller hint if allowed.

    `reasons` stays empty on the silent default (sync, no hint) so existing sync responses
    are unchanged; any async decision or ignored hint leaves an explicit trace.
    """
    policy = policies.get(category or "") or policies.get(DEFAULT_POLICY_CATEGORY)
    if policy is None:
        return ResolvedExecution(
            execution_mode=EXECUTION_MODE_SYNC,
            policy_category=DEFAULT_POLICY_CATEGORY,
            reasons=["no execution policy found — defaulting to sync"],
        )
    mode = policy.execution_mode
    reasons: list[str] = []
    if requested_mode is not None and requested_mode != mode:
        if policy.override_allowed:
            mode = requested_mode
            reasons.append(
                f"execution mode: {mode} (caller processing_mode honored, "
                f"policy '{policy.category}' allows override)"
            )
        else:
            reasons.append(
                f"processing_mode '{requested_mode}' ignored — policy "
                f"'{policy.category}' locks {policy.execution_mode}"
            )
    if mode != EXECUTION_MODE_SYNC and not any(
        reason.startswith("execution mode:") for reason in reasons
    ):
        reasons.append(f"execution mode: {mode} (policy '{policy.category}')")
    return ResolvedExecution(
        execution_mode=mode, policy_category=policy.category, reasons=reasons
    )


class ExecutionPolicyRepository(ABC):
    """The policy store seam. IT swaps the SQLite proxy for the internal DB; the door reads it,
    the /policies UI writes it (one writer, no scheduler involved)."""

    @abstractmethod
    def upsert(self, policy: ExecutionPolicy) -> None:
        """Insert or replace the policy row for `policy.category`."""

    @abstractmethod
    def get(self, category: str) -> ExecutionPolicy | None: ...

    @abstractmethod
    def all_policies(self) -> list[ExecutionPolicy]:
        """Every policy row, default ('*') included."""

    @abstractmethod
    def delete(self, category: str) -> bool:
        """Remove a category's row (it falls back to '*'). Returns False if absent."""

    @abstractmethod
    def seed_defaults(self) -> int:
        """Insert the in-code DEFAULT_EXECUTION_POLICIES rows that are MISSING (never
        overwrites an operator edit). Returns how many were inserted. Idempotent."""

    @abstractmethod
    def close(self) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_execution_policies (
    category         TEXT PRIMARY KEY,
    execution_mode   TEXT NOT NULL,
    override_allowed INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
"""


class SqliteExecutionPolicyRepository(ExecutionPolicyRepository):
    """The jettisonable SQLite proxy — same table shape IT will build on the internal target DB
    (explicit timestamps, TINYINT-like flag, short PK)."""

    def __init__(self, store: Store | str | Path = "ocr_store.sqlite") -> None:
        self._store = store if isinstance(store, Store) else Store(store)
        self._connection = self._store.connection
        self._clock = self._store.clock
        self._store.ensure_schema(_SCHEMA)

    def upsert(self, policy: ExecutionPolicy) -> None:
        if policy.execution_mode not in EXECUTION_MODES:
            raise ValueError(f"unknown execution_mode: {policy.execution_mode!r}")
        now = self._clock()
        existing = self._connection.execute(
            "SELECT created_at FROM ocr_execution_policies WHERE category = ?",
            (policy.category,),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        self._connection.execute(
            "INSERT OR REPLACE INTO ocr_execution_policies "
            "(category, execution_mode, override_allowed, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (
                policy.category,
                policy.execution_mode,
                1 if policy.override_allowed else 0,
                created_at,
                now,
            ),
        )
        self._connection.commit()

    def _row_to_policy(self, row: sqlite3.Row) -> ExecutionPolicy:
        return ExecutionPolicy(
            category=row["category"],
            execution_mode=row["execution_mode"],
            override_allowed=bool(row["override_allowed"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, category: str) -> ExecutionPolicy | None:
        row = self._connection.execute(
            "SELECT * FROM ocr_execution_policies WHERE category = ?", (category,)
        ).fetchone()
        return self._row_to_policy(row) if row else None

    def all_policies(self) -> list[ExecutionPolicy]:
        rows = self._connection.execute(
            "SELECT * FROM ocr_execution_policies ORDER BY category"
        ).fetchall()
        return [self._row_to_policy(row) for row in rows]

    def delete(self, category: str) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM ocr_execution_policies WHERE category = ?", (category,)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def seed_defaults(self) -> int:
        inserted = 0
        for default_policy in DEFAULT_EXECUTION_POLICIES:
            if self.get(default_policy.category) is None:
                self.upsert(default_policy)
                inserted += 1
        return inserted

    def close(self) -> None:
        self._connection.close()
