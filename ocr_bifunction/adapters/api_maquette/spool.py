"""The waiting room: a submission's bytes on disk while async work reaches them.

A `needs_review` row keeps its spool so the reviewer SEES the document next to its
extraction; every other terminal state purges it (PII hygiene, one owner per phase)."""

from __future__ import annotations

import uuid
from pathlib import Path


from ocr_bifunction.storage.repository import (
    Job,
)

from ocr_bifunction.adapters.api_maquette.settings import (
    DEFAULT_SUFFIX,
    SPOOL_ROOT,
)

# --- Escalation lane: spool the bytes, write a `received` row — the watchdog does the rest.


def _write_files(files: list[tuple[str, bytes]], directory: Path) -> list[Path]:
    """Write (filename, bytes) uploads to a directory, preserving each file's suffix."""
    paths: list[Path] = []
    for index, (filename, data) in enumerate(files):
        suffix = Path(filename).suffix or DEFAULT_SUFFIX
        file_path = directory / f"file_{index}{suffix}"
        file_path.write_bytes(data)
        paths.append(file_path)
    return paths


def _spool_files(files: list[tuple[str, bytes]]) -> str:
    """Persist one submission's bytes to a fresh spool directory; return its path.

    The spool is the document's WAITING ROOM: async work reads it, and a `needs_review`
    row keeps it so the reviewer can SEE the document next to its extraction. The
    watchdog purges it at every terminal state except needs_review; the sweep purges it
    when the human decision closes the job (PII hygiene, one owner per phase)."""
    spool_directory = SPOOL_ROOT / f"sub_{uuid.uuid4().hex[:12]}"
    spool_directory.mkdir(parents=True, exist_ok=False)
    _write_files(files, spool_directory)
    return str(spool_directory)


def _spooled_document_files(job: Job) -> list[Path]:
    """The job's spooled files, [] when nothing is retained (purged or never spooled)."""
    if not job.document_ref:
        return []
    spool_directory = Path(job.document_ref)
    if not spool_directory.is_dir():
        return []
    return sorted(path for path in spool_directory.iterdir() if path.is_file())
