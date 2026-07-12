"""Capacity settings — the door's admission levers (infra config, hardware-dependent).

Worst-case decision (user, 2026-07-12): the target servers are modest (no GPU racks),
so the sync lane must be CAPPED and the cap must be CONFIGURABLE — same governance as
the execution policies (« comme nous avons déjà mis en place pour sync/async/SLM ») so
the values adapt to the real day-J hardware without a redeploy.

Two levers (the fabrique « leviers » pattern: defaults IN CODE, keyed seed in the DB,
overrides read at runtime):

  - SYNC_CONCURRENCY_LIMIT — how many uploads may be PROCESSED synchronously at the
    same time. Beyond it the door never melts: it applies the overflow action. Default
    2 (physical cores minus headroom on the 4-core/8GB reference machine; raise it on
    beefier prod hardware).
  - SYNC_OVERFLOW_ACTION — what a saturated door does with the next upload:
      defer      — spool + D1 `received` on the 'deferred' lane, answered 202 pending
                   (the bi-mode is the pressure valve: sync when possible, async when
                   loaded). The default.
      reject_503 — refuse with HTTP 503 + Retry-After (the client retries; nothing is
                   queued on our side).

The store is a generic key/value table (`ocr_capacity_settings`) so future infra levers
join without a schema change. Unknown/broken stored values fall back to the in-code
default (a corrupted lever must never take the door down); the PUT endpoint validates,
so bad values cannot enter through the API.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

OVERFLOW_ACTION_DEFER = "defer"
OVERFLOW_ACTION_REJECT_503 = "reject_503"
OVERFLOW_ACTIONS = (OVERFLOW_ACTION_DEFER, OVERFLOW_ACTION_REJECT_503)

# In-code defaults — the seed. Keys are the levers' names, values their defaults.
DEFAULT_CAPACITY_SETTINGS = {
    "SYNC_CONCURRENCY_LIMIT": "2",
    "SYNC_OVERFLOW_ACTION": OVERFLOW_ACTION_DEFER,
}


@dataclass
class CapacitySettings:
    """The parsed, validated levers the door reads per request."""

    sync_concurrency_limit: int
    sync_overflow_action: str


class CapacitySettingsRepository(ABC):
    """The levers store seam. Ops edits it (/policies page); the door reads it."""

    @abstractmethod
    def get(self, setting_key: str) -> str | None: ...

    @abstractmethod
    def upsert(self, setting_key: str, setting_value: str) -> None: ...

    @abstractmethod
    def all_settings(self) -> dict[str, str]: ...

    @abstractmethod
    def seed_defaults(self) -> int:
        """Insert the MISSING in-code defaults (never overwrites an edit)."""

    @abstractmethod
    def close(self) -> None: ...


def load_capacity_settings(repository: CapacitySettingsRepository) -> CapacitySettings:
    """Parse the stored levers, falling back to the in-code default on a broken value
    (a corrupted lever must never take the door down — the PUT endpoint validates, so
    this guards only direct store edits)."""
    stored = repository.all_settings()
    try:
        limit = int(stored.get("SYNC_CONCURRENCY_LIMIT", ""))
        if limit < 1:
            raise ValueError
    except ValueError:
        limit = int(DEFAULT_CAPACITY_SETTINGS["SYNC_CONCURRENCY_LIMIT"])
    action = stored.get("SYNC_OVERFLOW_ACTION", "")
    if action not in OVERFLOW_ACTIONS:
        action = DEFAULT_CAPACITY_SETTINGS["SYNC_OVERFLOW_ACTION"]
    return CapacitySettings(sync_concurrency_limit=limit, sync_overflow_action=action)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_capacity_settings (
    setting_key   TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


class SqliteCapacitySettingsRepository(CapacitySettingsRepository):
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

    def get(self, setting_key: str) -> str | None:
        row = self._connection.execute(
            "SELECT setting_value FROM ocr_capacity_settings WHERE setting_key = ?",
            (setting_key,),
        ).fetchone()
        return row["setting_value"] if row else None

    def upsert(self, setting_key: str, setting_value: str) -> None:
        now = self._clock()
        existing = self._connection.execute(
            "SELECT created_at FROM ocr_capacity_settings WHERE setting_key = ?",
            (setting_key,),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        self._connection.execute(
            "INSERT OR REPLACE INTO ocr_capacity_settings "
            "(setting_key, setting_value, created_at, updated_at) VALUES (?,?,?,?)",
            (setting_key, setting_value, created_at, now),
        )
        self._connection.commit()

    def all_settings(self) -> dict[str, str]:
        rows = self._connection.execute(
            "SELECT setting_key, setting_value FROM ocr_capacity_settings"
        ).fetchall()
        return {row["setting_key"]: row["setting_value"] for row in rows}

    def seed_defaults(self) -> int:
        inserted = 0
        for setting_key, setting_value in DEFAULT_CAPACITY_SETTINGS.items():
            if self.get(setting_key) is None:
                self.upsert(setting_key, setting_value)
                inserted += 1
        return inserted

    def close(self) -> None:
        self._connection.close()
