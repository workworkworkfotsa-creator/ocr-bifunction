"""Use-case API keys — the door's FIRST auth surface, and the seam that resolves which
consumer profile a request belongs to (D7, née 2026-07-20).

Two real consumers of the reading service exist by design (CADRAGE-META "enveloppe à
profondeur variable, clef à la porte"): `ci_pii` (CI/habilitation validation, today's
unchanged behaviour) and `sop_contract` (SOP/contract reconciliation, its reader not yet
built). The KEY resolves which profile a caller is — never the output SHAPE, which stays
one schema for every use_case (only its fill-depth varies, a later concern once the SOP
reader exists). This module only does the auth + resolution; it deliberately does NOT
fork any processing behaviour by use_case yet (no reader consumes `sop_contract`
differently — that would be inert, un-exercised code).

Zero-regression by construction: a request with NO key resolves to `DEFAULT_USE_CASE`
(`ci_pii`), silently (no trace), matching every caller before this module existed
(mirrors `resolve_execution`'s silent-default rule). A key that IS presented but unknown
or revoked resolves to `use_case=None` — the door turns that into a 401, never a
silent fallback (a real auth guarantee, not merely a hint).

The raw key is generated once, shown to the caller ONCE, and never stored: only its
SHA-256 hash lives in `ocr_use_case_keys`. SQLite is the jettisonable proxy of the
internal target-DB table (explicit timestamps, portable shape).
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ocr_bifunction.store import Store

USE_CASE_CI_PII = "ci_pii"
USE_CASE_SOP_CONTRACT = "sop_contract"
KNOWN_USE_CASES = (USE_CASE_CI_PII, USE_CASE_SOP_CONTRACT)

# No key presented -> this use_case, silently (the behaviour every caller already gets).
DEFAULT_USE_CASE = USE_CASE_CI_PII


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    """A fresh caller-facing secret. Never persisted — only `hash_key(...)` of it is."""
    return secrets.token_urlsafe(32)


@dataclass
class UseCaseKey:
    """One issued key: its hash (never the raw secret) + which use_case it resolves to.

    `raw_key` is populated ONLY by `create()`'s return value (the one-time reveal to the
    caller) — never by a row read back from storage, since the raw secret is never
    persisted."""

    key_hash: str
    label: str
    use_case: str
    key_id: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    raw_key: str | None = None


@dataclass
class ResolvedUseCase:
    """The door's decision for one request. `use_case` is None only for an unknown/revoked
    key — the caller (the door) turns that into a 401; this module never raises HTTP."""

    use_case: str | None
    key_label: str | None = None
    reasons: list[str] = field(default_factory=list)


def resolve_use_case(
    raw_key: str | None, repository: "UseCaseKeyRepository"
) -> ResolvedUseCase:
    """Pure resolution: no key -> DEFAULT_USE_CASE (silent, zero-regression). A key that
    matches no active hash -> `use_case=None` (the door rejects, never falls back)."""
    if raw_key is None:
        return ResolvedUseCase(use_case=DEFAULT_USE_CASE)
    key = repository.get_by_hash(hash_key(raw_key))
    if key is None:
        return ResolvedUseCase(use_case=None, reasons=["unknown or revoked API key"])
    return ResolvedUseCase(
        use_case=key.use_case,
        key_label=key.label,
        reasons=[f"use_case '{key.use_case}' resolved from API key '{key.label}'"],
    )


class UseCaseKeyRepository(ABC):
    """The key store seam. An operator issues/revokes keys (`/use-case-keys` UI); the
    door only ever reads by hash, on every request."""

    @abstractmethod
    def create(self, label: str, use_case: str) -> UseCaseKey:
        """Generate + persist a new key (hash only) and return it with `.raw_key` set —
        the caller must read + display it now; it is never retrievable again."""

    @abstractmethod
    def get_by_hash(self, key_hash: str) -> UseCaseKey | None: ...

    @abstractmethod
    def all_keys(self) -> list[UseCaseKey]:
        """Every issued key (hash included, never the raw secret — it was never stored)."""

    @abstractmethod
    def revoke(self, key_id: int) -> bool:
        """Remove a key. Returns False if absent. Past D1 rows already carry a snapshot
        of the use_case they resolved to — revoking a key never rewrites history."""

    @abstractmethod
    def close(self) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_use_case_keys (
    key_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash   TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL,
    use_case   TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ocr_use_case_keys_hash ON ocr_use_case_keys (key_hash);
"""


class SqliteUseCaseKeyRepository(UseCaseKeyRepository):
    """The jettisonable SQLite proxy — same table shape IT will build on the internal
    target DB. The DB file holds only hashes (no secret at rest) and is gitignored."""

    def __init__(self, store: Store | str | Path = "ocr_store.sqlite") -> None:
        self._store = store if isinstance(store, Store) else Store(store)
        self._connection = self._store.connection
        self._clock = self._store.clock
        self._store.ensure_schema(_SCHEMA)

    def create(self, label: str, use_case: str) -> UseCaseKey:
        if use_case not in KNOWN_USE_CASES:
            raise ValueError(f"unknown use_case: {use_case!r}")
        raw_key = generate_key()
        now = self._clock()
        cursor = self._connection.execute(
            "INSERT INTO ocr_use_case_keys (key_hash, label, use_case, created_at, "
            "updated_at) VALUES (?,?,?,?,?)",
            (hash_key(raw_key), label, use_case, now, now),
        )
        self._connection.commit()
        return UseCaseKey(
            key_id=int(cursor.lastrowid),
            key_hash=hash_key(raw_key),
            label=label,
            use_case=use_case,
            created_at=now,
            updated_at=now,
            raw_key=raw_key,
        )

    def _row_to_key(self, row: sqlite3.Row) -> UseCaseKey:
        return UseCaseKey(
            key_id=row["key_id"],
            key_hash=row["key_hash"],
            label=row["label"],
            use_case=row["use_case"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_by_hash(self, key_hash: str) -> UseCaseKey | None:
        row = self._connection.execute(
            "SELECT * FROM ocr_use_case_keys WHERE key_hash = ?", (key_hash,)
        ).fetchone()
        return self._row_to_key(row) if row else None

    def all_keys(self) -> list[UseCaseKey]:
        rows = self._connection.execute(
            "SELECT * FROM ocr_use_case_keys ORDER BY key_id"
        ).fetchall()
        return [self._row_to_key(row) for row in rows]

    def revoke(self, key_id: int) -> bool:
        cursor = self._connection.execute(
            "DELETE FROM ocr_use_case_keys WHERE key_id = ?", (key_id,)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._connection.close()
