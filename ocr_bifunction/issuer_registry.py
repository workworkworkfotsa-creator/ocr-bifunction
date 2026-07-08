"""Issuer registry — the curated list of recognized training organisms (D-e plumbing).

The anti-fraud regime for `attestation_formation` rests on a REGISTRY: the issuer read
on the document (SIRET preferred over a copyable name) must belong to a list a human
curates — "ma mère peut me faire une certif" dies here. The `issuer_registry` check
(template.py) already exists and fails loud without its state; this module IS that
state: a small table the Backoffice edits (métier surface — the expert owns the
content, IT owns only the store), read at validation time into
`ValidationContext.issuer_registry`.

An EMPTY registry yields context `None` for that check -> needs_review, never a false
pass (an absent registry cannot prove an issuer legitimate). SQLite is the jettisonable
proxy of the MariaDB table (explicit timestamps, 5.5-safe shape).
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class IssuerEntry:
    """One recognized organism: its identifier (SIRET preferred) and a human label."""

    identifier: str
    label: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class IssuerRegistryRepository(ABC):
    """The registry store seam. The Backoffice curates it (UI writes); validation reads."""

    @abstractmethod
    def upsert(self, entry: IssuerEntry) -> None: ...

    @abstractmethod
    def get(self, identifier: str) -> IssuerEntry | None: ...

    @abstractmethod
    def all_entries(self) -> list[IssuerEntry]: ...

    @abstractmethod
    def delete(self, identifier: str) -> bool:
        """Remove an organism. Returns False if absent."""

    @abstractmethod
    def close(self) -> None: ...

    def identifiers(self) -> frozenset[str] | None:
        """What ValidationContext.issuer_registry consumes: the identifier set, or None
        when the registry is EMPTY (fail-loud review, never an empty-set false proof)."""
        entries = self.all_entries()
        if not entries:
            return None
        return frozenset(entry.identifier for entry in entries)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_issuer_registry (
    identifier TEXT PRIMARY KEY,
    label      TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class SqliteIssuerRegistryRepository(IssuerRegistryRepository):
    """The jettisonable SQLite proxy — same table shape IT will build in MariaDB 5.5."""

    def __init__(
        self,
        database_path: str | Path = "ocr_store.sqlite",
        *,
        clock: Callable[[], str] | None = None,
        check_same_thread: bool = True,
    ) -> None:
        self._clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self._connection = sqlite3.connect(
            str(database_path), check_same_thread=check_same_thread
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def upsert(self, entry: IssuerEntry) -> None:
        now = self._clock()
        existing = self._connection.execute(
            "SELECT created_at FROM ocr_issuer_registry WHERE identifier = ?",
            (entry.identifier,),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        self._connection.execute(
            "INSERT OR REPLACE INTO ocr_issuer_registry "
            "(identifier, label, created_at, updated_at) VALUES (?,?,?,?)",
            (entry.identifier, entry.label, created_at, now),
        )
        self._connection.commit()

    def _row_to_entry(self, row: sqlite3.Row) -> IssuerEntry:
        return IssuerEntry(
            identifier=row["identifier"],
            label=row["label"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get(self, identifier: str) -> IssuerEntry | None:
        row = self._connection.execute(
            "SELECT * FROM ocr_issuer_registry WHERE identifier = ?", (identifier,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def all_entries(self) -> list[IssuerEntry]:
        rows = self._connection.execute(
            "SELECT * FROM ocr_issuer_registry ORDER BY identifier"
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def delete(self, identifier: str) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM ocr_issuer_registry WHERE identifier = ?", (identifier,)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._connection.close()
