"""Non-conformity policy — WHAT a proven non-conformity does, per category (métier config).

Terminology decision (user, 2026-07-12): the machine never proves FRAUD (an intent —
compliance's judgment); it proves a document NON-CONFORME: broken checksum, incoherent
dates, issuer outside the registry, declared type != recognized type. The technical wire
status stays `rejected`; the surfaces say « document non conforme » and the evidence
(template, computed checks, reasons, retained bytes) goes to the human review.

What was missing (user, 2026-07-12): the REACTION is a métier choice per category —
« dans ces cas-là on bloque les uploads suivants, ou pas, ou on flag mais le process
continue » — three actions:

  - block            — THIS upload is refused (terminal non conforme). The default.
  - block_holder     — same, AND subsequent uploads DECLARING the same holder are
                       refused while a non-conformity of that holder is still OPEN
                       (no review decision yet). Clearing it at the review unblocks.
  - flag_and_continue — nothing is blocked: the non-conformity is FLAGGED in the
                       reasons and the document routes to human review; the process
                       continues.

Same governance as the execution policies: defaults IN CODE, seed inserts missing rows
only (operator edits survive), '*' is the fallback row, edited through the /policies
page with immediate effect. SQLite is the jettisonable proxy of the MariaDB table.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ocr_bifunction.store import Store

CONFORMITY_ACTION_BLOCK = "block"
CONFORMITY_ACTION_BLOCK_HOLDER = "block_holder"
CONFORMITY_ACTION_FLAG_AND_CONTINUE = "flag_and_continue"
CONFORMITY_ACTIONS = (
    CONFORMITY_ACTION_BLOCK,
    CONFORMITY_ACTION_BLOCK_HOLDER,
    CONFORMITY_ACTION_FLAG_AND_CONTINUE,
)

DEFAULT_CONFORMITY_CATEGORY = "*"


@dataclass
class ConformityPolicy:
    """One row of the surface: what a proven non-conformity triggers for a category."""

    category: str  # a document category, or '*' for the default row
    action: str  # one of CONFORMITY_ACTIONS
    created_at: str | None = None
    updated_at: str | None = None


# In-code default (the seed): block the non-conforming upload itself, nothing more —
# the historical behavior. Hardening (block_holder) or softening (flag_and_continue)
# is a métier decision made through the surface, never assumed here.
DEFAULT_CONFORMITY_POLICIES = (
    ConformityPolicy(
        category=DEFAULT_CONFORMITY_CATEGORY, action=CONFORMITY_ACTION_BLOCK
    ),
)


def resolve_conformity_action(
    category: str | None, policies: dict[str, ConformityPolicy]
) -> str:
    """The action for a category: its own row, else '*', else the in-code default."""
    policy = policies.get(category or "") or policies.get(DEFAULT_CONFORMITY_CATEGORY)
    return policy.action if policy is not None else CONFORMITY_ACTION_BLOCK


class ConformityPolicyRepository(ABC):
    """The store seam. The métier edits it (/policies page); the door and the watchdog
    read it when a non-conformity fires."""

    @abstractmethod
    def upsert(self, policy: ConformityPolicy) -> None: ...

    @abstractmethod
    def get(self, category: str) -> ConformityPolicy | None: ...

    @abstractmethod
    def all_policies(self) -> list[ConformityPolicy]: ...

    @abstractmethod
    def delete(self, category: str) -> bool:
        """Remove a category's row (falls back to '*'). Returns False if absent."""

    @abstractmethod
    def seed_defaults(self) -> int:
        """Insert the MISSING in-code default rows (never overwrites an edit)."""

    @abstractmethod
    def close(self) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_conformity_policies (
    category   TEXT PRIMARY KEY,
    action     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class SqliteConformityPolicyRepository(ConformityPolicyRepository):
    """The jettisonable SQLite proxy — same table shape IT will build in MariaDB 5.5."""

    def __init__(self, store: Store | str | Path = "ocr_store.sqlite") -> None:
        self._store = store if isinstance(store, Store) else Store(store)
        self._connection = self._store.connection
        self._clock = self._store.clock
        self._store.ensure_schema(_SCHEMA)

    def upsert(self, policy: ConformityPolicy) -> None:
        if policy.action not in CONFORMITY_ACTIONS:
            raise ValueError(f"unknown conformity action: {policy.action!r}")
        now = self._clock()
        existing = self._connection.execute(
            "SELECT created_at FROM ocr_conformity_policies WHERE category = ?",
            (policy.category,),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        self._connection.execute(
            "INSERT OR REPLACE INTO ocr_conformity_policies "
            "(category, action, created_at, updated_at) VALUES (?,?,?,?)",
            (policy.category, policy.action, created_at, now),
        )
        self._connection.commit()

    def _row_to_policy(self, row: sqlite3.Row) -> ConformityPolicy:
        return ConformityPolicy(
            category=row["category"],
            action=row["action"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, category: str) -> ConformityPolicy | None:
        row = self._connection.execute(
            "SELECT * FROM ocr_conformity_policies WHERE category = ?", (category,)
        ).fetchone()
        return self._row_to_policy(row) if row else None

    def all_policies(self) -> list[ConformityPolicy]:
        rows = self._connection.execute(
            "SELECT * FROM ocr_conformity_policies ORDER BY category"
        ).fetchall()
        return [self._row_to_policy(row) for row in rows]

    def delete(self, category: str) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM ocr_conformity_policies WHERE category = ?", (category,)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def seed_defaults(self) -> int:
        inserted = 0
        for default_policy in DEFAULT_CONFORMITY_POLICIES:
            if self.get(default_policy.category) is None:
                self.upsert(default_policy)
                inserted += 1
        return inserted

    def close(self) -> None:
        self._connection.close()
