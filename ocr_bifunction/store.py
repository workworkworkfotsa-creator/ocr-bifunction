"""The DB Store — one connection, the schema/migration mechanism, the clock (a thin adapter).

Before this, each of the 7 D1..D6 repos opened its OWN sqlite connection and copy-pasted the
same connect + `executescript` + PRAGMA-migrate + commit block. The Store owns that mechanism
ONCE and hands the connection to every repo that shares it.

Sharing one connection is also what makes an in-memory store possible: separate connections to
`":memory:"` are separate EMPTY databases, so the repos must share ONE connection for a
`Store(":memory:")` to hold all their tables together — the cheap, disk-free, real-SQL test
seam (it runs the exact SQL prod runs, so it cannot "lie" the way a hand-written fake could).
Prod passes a file path; the future internal-DB store is a second adapter at this same seam.

Repos keep their OWN table DDL (locality — a table's shape lives next to the repo that uses it);
the Store only RUNS it. Timestamps stay explicit (the target DB may lack DEFAULT CURRENT_TIMESTAMP);
the clock is injectable here, once, for reproducible tests.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


class Store:
    """One SQLite connection shared by the repos, plus the schema/migration mechanism.

    `database` is a file path (prod) or `":memory:"` (in-process tests). `check_same_thread`
    is False when request threads and the async worker share one connection — the caller then
    serializes access with a lock (the API does), covering the dropped per-thread guard."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        clock: Callable[[], str] | None = None,
        check_same_thread: bool = True,
    ) -> None:
        self._clock = clock or (lambda: datetime.now().isoformat(timespec="seconds"))
        self.connection = sqlite3.connect(
            str(database), check_same_thread=check_same_thread
        )
        self.connection.row_factory = sqlite3.Row

    def clock(self) -> str:
        """The explicit timestamp a repo stamps on created_at/updated_at."""
        return self._clock()

    def ensure_schema(
        self,
        schema_sql: str,
        *,
        table: str | None = None,
        migrations: dict[str, str] | None = None,
    ) -> None:
        """Run a repo's DDL on the shared connection, then apply its proxy-only column
        migrations. `table` + `migrations` add any columns missing from an existing local
        `.sqlite` (PRAGMA table_info) — the ALTER loop once copy-pasted across the D1/D2/D3
        repos, now in one place. The target-DB DDL at handoff bakes these in; this is proxy-only."""
        self.connection.executescript(schema_sql)
        if table and migrations:
            existing_columns = {
                row["name"]
                for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            for column_name, alter_statement in migrations.items():
                if column_name not in existing_columns:
                    self.connection.execute(alter_statement)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
