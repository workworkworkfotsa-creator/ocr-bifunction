"""Watchdog worker — the SEPARATE process that owns every D1 status transition.

    uv run python worker_watchdog.py                 # loop (Ctrl+C to stop after current job)
    uv run python worker_watchdog.py --once          # one pass and exit (cron parity + smokes)
    uv run python worker_watchdog.py --fake-escalation   # smoke seam: no VLM, no llama

The queue IS the table (repository.py doctrine): a doubtful submission is a D1 row
`status='received'` whose bytes wait in the spool (`document_ref`). This worker polls, claims,
processes, and closes — the API door only ever INSERTS rows; the review UI only writes D3.

One pass does, in order:
  1. RECOVER  — `processing` rows whose lease expired (a worker crashed mid-job) go back to
                `received` for a retry, or `failed` once `attempts` hits the cap (poison pill).
  2. DRAIN    — claim the oldest `received` escalation row (ATOMIC: two workers can never take
                the same row), re-run the CI submission WITH the escalation engine from the
                spooled files, write the terminal state, purge the spool dir (PII hygiene).
                Strictly ONE job at a time (8 GB target: never two heavy engines at once).
  3. SWEEP    — D3 decisions (accept/reject) on jobs still `needs_review` close those D1 rows
                (accept -> done, reject -> failed): the UI wrote D3, THIS process writes D1.

Not-stepping-on-toes, four locks: a PID lockfile (only one watchdog; if it is stale after a
crash, delete it), the atomic claim, the sequential loop, and one-writer-per-column.

No PII in this file; the spool and store are gitignored. --fake-escalation lets every smoke
run WITHOUT llama (the escalation SHAPE is proven; the VLM quality was proven separately).
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from pathlib import Path

from ocr_bifunction.pipeline import CiRecord, process_ci_submission
from ocr_bifunction.reader import OcrEngine, TextLine
from ocr_bifunction.repository import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    Job,
    SqliteRepository,
)
from ocr_bifunction.review_repository import (
    DECISION_ACCEPT,
    SqliteReviewRepository,
)

PROJECT_ROOT = Path(__file__).parent
TEMPLATES_DIRECTORY = PROJECT_ROOT / "templates"


class _LazyRapidOcrEngine:
    """Build RapidOCR on first use (the worker may sweep decisions without ever reading a doc)."""

    name = "rapidocr(lazy)"

    def __init__(self) -> None:
        self._engine = None

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        if self._engine is None:
            from ocr_bifunction.rapidocr_engine import RapidOcrEngine

            self._engine = RapidOcrEngine()
            self.name = self._engine.name
        return self._engine.recognize(image_png_bytes)


class _FakeEscalationEngine:
    """Smoke stand-in for the VLM: returns no lines, so the escalation SHAPE runs in seconds
    without llama. The terminal state is then honest (`needs_review`, 'no MRZ')."""

    name = "fake-escalation"

    def recognize(self, image_png_bytes: bytes) -> list[TextLine]:
        return []


def _build_escalation_engine(fake: bool) -> OcrEngine:
    if fake:
        return _FakeEscalationEngine()
    from ocr_bifunction.lightonocr_engine import LightOnOcrEngine

    return LightOnOcrEngine()


def _terminal_from_record(
    record: CiRecord | None,
) -> tuple[str, str | None, dict[str, str | None] | None, list[str]]:
    """Map an escalated result to a D1 terminal state (same bridge as the batch): auto ->
    done/auto; doubtful -> needs_review/human. Verso-read provenance folds into reasons."""
    if record is None:
        return STATUS_NEEDS_REVIEW, None, None, ["escalation produced no record"]
    reasons = [*record.reasons, f"verso read via: {record.verso_read_path}"]
    if record.verdict == "auto":
        return STATUS_DONE, "auto", record.fields, reasons
    return STATUS_NEEDS_REVIEW, "human", record.fields, reasons


def _process_claimed_job(
    job: Job,
    repository: SqliteRepository,
    fast_engine: OcrEngine,
    escalation_engine: OcrEngine,
) -> None:
    """Re-run the spooled submission WITH the escalation engine; terminal state + spool purge."""
    try:
        spool_directory = Path(job.document_ref) if job.document_ref else None
        if spool_directory is None or not spool_directory.is_dir():
            raise FileNotFoundError(f"spool missing: {job.document_ref!r}")
        source_paths = sorted(
            path for path in spool_directory.iterdir() if path.is_file()
        )
        result = process_ci_submission(
            source_paths,
            fast_engine,
            TEMPLATES_DIRECTORY,
            category=job.category,
            escalation_engine=escalation_engine,
        )
        status_value, verdict, fields, reasons = _terminal_from_record(result.record)
        repository.update_status(
            job.job_id,
            status_value,
            verdict=verdict,
            record_fields=fields,
            reasons=reasons,
        )
        print(f"  job #{job.job_id}: processed -> {status_value}")
    except Exception as error:  # terminal failure lands IN the row, never hidden
        repository.update_status(
            job.job_id,
            STATUS_FAILED,
            reasons=[f"escalation failure: {type(error).__name__}: {error}"],
        )
        print(f"  job #{job.job_id}: FAILED ({type(error).__name__})")
    finally:
        if job.document_ref:
            shutil.rmtree(job.document_ref, ignore_errors=True)  # PII leaves the disk


def _sweep_decisions(
    repository: SqliteRepository, review_repository: SqliteReviewRepository
) -> int:
    """Close D1 jobs whose review carries a human decision: accept -> done, reject -> failed.

    Idempotent by construction: only jobs still `needs_review` are touched, so a re-sweep of
    the same decision is a no-op. THIS process writes D1; the UI only wrote D3."""
    closed = 0
    for review in review_repository.decided():
        job = repository.get(review.job_id)
        if job is None or job.status != STATUS_NEEDS_REVIEW:
            continue
        if review.decision == DECISION_ACCEPT:
            repository.update_status(
                job.job_id,
                STATUS_DONE,
                reasons=[*job.reasons, "human decision: accept"],
            )
            print(
                f"  job #{job.job_id}: closed done (human accepted, review #{review.review_id})"
            )
        else:
            repository.update_status(
                job.job_id,
                STATUS_FAILED,
                reasons=[*job.reasons, "human decision: reject (rescan requested)"],
            )
            print(
                f"  job #{job.job_id}: closed failed (human rejected, review #{review.review_id})"
            )
        closed += 1
    return closed


def _one_pass(
    repository: SqliteRepository,
    review_repository: SqliteReviewRepository,
    fast_engine: OcrEngine,
    escalation_engine: OcrEngine,
    lease_seconds: float,
    max_attempts: int,
) -> int:
    """Recover -> drain (all queued, one at a time) -> sweep. Returns jobs processed."""
    requeued, gave_up = repository.recover_stale(lease_seconds, max_attempts)
    if requeued or gave_up:
        print(f"  recover: {requeued} requeued, {gave_up} gave up (stale leases)")
    processed = 0
    while True:
        job = repository.claim_next("escalation")
        if job is None:
            break
        print(f"  claimed job #{job.job_id} (attempt {job.attempts}) <- {job.source}")
        _process_claimed_job(job, repository, fast_engine, escalation_engine)
        processed += 1
    _sweep_decisions(repository, review_repository)
    return processed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Watchdog worker: drain the D1 queue (the table IS the queue)."
    )
    parser.add_argument(
        "--store", default=os.environ.get("OCR_STORE_PATH", "ocr_store.sqlite")
    )
    parser.add_argument(
        "--once", action="store_true", help="One pass and exit (cron parity)."
    )
    parser.add_argument(
        "--interval", type=float, default=5.0, help="Loop poll seconds."
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=1800.0,
        help="A `processing` row older than this is considered crashed (VLM ~8 min/img).",
    )
    parser.add_argument(
        "--max-attempts", type=int, default=3, help="Poison-pill cap before `failed`."
    )
    parser.add_argument(
        "--fake-escalation",
        action="store_true",
        help="Use a no-op escalation engine (smokes: no llama, no VLM).",
    )
    parser.add_argument("--pid-file", type=Path, default=Path("watchdog.pid"))
    arguments = parser.parse_args()

    # PID lock: exactly ONE watchdog. 'x' mode is an atomic create-or-fail; a crashed
    # watchdog leaves the file behind -> the operator deletes it (message says so).
    try:
        with open(arguments.pid_file, "x", encoding="utf-8") as pid_file:
            pid_file.write(str(os.getpid()))
    except FileExistsError:
        print(
            f"REFUSED: {arguments.pid_file} exists — another watchdog is running "
            "(or crashed; delete the file if you are sure none is)."
        )
        return 2

    repository = SqliteRepository(arguments.store)
    review_repository = SqliteReviewRepository(arguments.store)
    fast_engine = _LazyRapidOcrEngine()
    escalation_engine = _build_escalation_engine(arguments.fake_escalation)
    print(
        f"watchdog up (pid {os.getpid()}): store={arguments.store}, "
        f"escalation={escalation_engine.name}, {'once' if arguments.once else 'loop'}"
    )
    try:
        while True:
            _one_pass(
                repository,
                review_repository,
                fast_engine,
                escalation_engine,
                arguments.lease_seconds,
                arguments.max_attempts,
            )
            if arguments.once:
                break
            time.sleep(arguments.interval)
    except KeyboardInterrupt:
        print("watchdog stopping (Ctrl+C).")
    finally:
        repository.close()
        review_repository.close()
        arguments.pid_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
