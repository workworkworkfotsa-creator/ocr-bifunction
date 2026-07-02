"""D2 — the templates store (the dictionnaire métier), behind a TemplateRepository ABC.

The template library made a real store. Per the fabrique doctrine (skill handoff-it) the CONTRACT
that crosses to IT is the TABLE; the storage engine is a jettisonable adapter behind a
`TemplateRepository` ABC (IT swaps `SqliteTemplateRepository` for a `MariaDbTemplateRepository`).

D2 EMERGES here, from the D3->D2 promotion (cf. HANDOFF plan): until a validated suggestion needed
to WRITE a template, the committed `templates/*.json` served reads fine (YAGNI). Now that the growth
loop writes templates, D2 becomes a store. A row = one template AND its validation criteria (the
criteria travel WITH the template — already the JSON's `validation` block; no separate table). The
committed JSON files are the anonymized SEED (`seed_from_directory`); the table is the runtime source.

`active_templates()` returns dicts in the SAME shape as the JSON templates, so `match_template` and
`extract_fields` (template.py) consume it UNCHANGED — we do not touch the read logic, we back it.

Owner (contrat-bd-destination.md): the Backoffice / métier expert curates D2; the async worker only
READS active templates. MariaDB target (co-freeze with IT): explicit timestamps, utf8, InnoDB,
`active` a TINYINT, index ≤767 o. SQLite is the proxy; the DB file may carry curated anchors and is
gitignored.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


class TemplateRepository(ABC):
    """The D2 store seam. IT swaps the SQLite proxy for a MariaDB implementation; the worker reads
    active templates through it, and promotion (D3->D2) writes through it."""

    @abstractmethod
    def upsert(self, template: dict, *, active: bool = True) -> None:
        """Insert a template or replace an existing one (by template_id), bumping its version."""

    @abstractmethod
    def get(self, template_id: str) -> dict | None:
        """One template in JSON shape (match/fields/validation), or None."""

    @abstractmethod
    def active_templates(self, category: str | None = None) -> list[dict]:
        """The ACTIVE templates in JSON shape (optionally one category) — what match_template reads."""

    @abstractmethod
    def seed_from_directory(self, directory: Path) -> int:
        """Import the committed `templates/*.json` as active rows; return how many. Idempotent seed."""

    @abstractmethod
    def close(self) -> None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ocr_templates (
    template_id     TEXT PRIMARY KEY,
    category        TEXT,
    match_json      TEXT,
    fields_json     TEXT,
    validation_json TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ocr_templates_active ON ocr_templates (active, category);
"""


class SqliteTemplateRepository(TemplateRepository):
    """The jettisonable SQLite proxy of D2 — same table shape IT will build in MariaDB.

    The match/fields/validation blocks are stored as JSON columns (they travel with the template);
    `active_templates` rebuilds the exact dict shape the committed JSON templates have, so the
    deterministic read path (match_template/extract_fields) is unchanged. Timestamps are explicit
    (MariaDB 5.5 has no DEFAULT CURRENT_TIMESTAMP); the clock is injectable for tests."""

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

    def upsert(self, template: dict, *, active: bool = True) -> None:
        template_id = template["template_id"]
        now = self._clock()
        existing = self._connection.execute(
            "SELECT version, created_at FROM ocr_templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        version = (existing["version"] + 1) if existing else 1
        self._connection.execute(
            "INSERT OR REPLACE INTO ocr_templates (template_id, category, match_json, "
            "fields_json, validation_json, active, version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                template_id,
                template.get("category"),
                json.dumps(template.get("match", {}), ensure_ascii=False),
                json.dumps(template.get("fields", []), ensure_ascii=False),
                json.dumps(template.get("validation", {}), ensure_ascii=False),
                1 if active else 0,
                version,
                created_at,
                now,
            ),
        )
        self._connection.commit()

    def _row_to_template(self, row: sqlite3.Row) -> dict:
        """Rebuild the JSON-template shape match_template/extract_fields expect."""
        return {
            "template_id": row["template_id"],
            "category": row["category"],
            "match": json.loads(row["match_json"] or "{}"),
            "fields": json.loads(row["fields_json"] or "[]"),
            "validation": json.loads(row["validation_json"] or "{}"),
        }

    def get(self, template_id: str) -> dict | None:
        row = self._connection.execute(
            "SELECT * FROM ocr_templates WHERE template_id = ?", (template_id,)
        ).fetchone()
        return self._row_to_template(row) if row else None

    def active_templates(self, category: str | None = None) -> list[dict]:
        query = "SELECT * FROM ocr_templates WHERE active = 1"
        parameters: list[object] = []
        if category is not None:
            query += " AND category = ?"
            parameters.append(category)
        query += " ORDER BY template_id"
        rows = self._connection.execute(query, parameters).fetchall()
        return [self._row_to_template(row) for row in rows]

    def seed_from_directory(self, directory: Path) -> int:
        count = 0
        for path in sorted(directory.glob("*.json")):
            self.upsert(json.loads(path.read_text(encoding="utf-8")), active=True)
            count += 1
        return count

    def close(self) -> None:
        self._connection.close()
