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
  2. DRAIN    — claim the oldest `received` row lane by lane (ATOMIC: two workers can never
                take the same row): 'escalation' (doubtful CI, re-run WITH the VLM) and
                'deferred' (execution policy async_immediate, routed through the 2-lane
                router) always; 'nightly' (policy async_nightly) only with `--nightly` —
                the night cron runs `--once --nightly`. Terminal state written, spool dir
                purged (PII hygiene). Strictly ONE job at a time (8 GB target).
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
from datetime import date
from pathlib import Path

from ocr_bifunction.pipeline import CiRecord, process_ci_submission
from ocr_bifunction.reader import OcrEngine, TextLine
from ocr_bifunction.repository import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_NEEDS_REVIEW,
    STATUS_REJECTED,
    Job,
    SqliteRepository,
)
from ocr_bifunction.review_repository import (
    DECISION_ACCEPT,
    SqliteReviewRepository,
)
from ocr_bifunction.drafting_flow import run_draft_pass
from ocr_bifunction.issuer_registry import SqliteIssuerRegistryRepository
from ocr_bifunction.router import route_document
from ocr_bifunction.template import ValidationContext
from ocr_bifunction.template_repository import SqliteTemplateRepository

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


# Routed-document verdict -> D1 terminal status (mirrors the API/batch bridge): auto ->
# done; human -> needs_review; reject -> rejected (proven invalid, anti-fraud, terminal).
_D1_STATUS_FOR_ROUTED_VERDICT = {
    "auto": STATUS_DONE,
    "human": STATUS_NEEDS_REVIEW,
    "reject": STATUS_REJECTED,
}


def _spooled_files(job: Job) -> list[Path]:
    spool_directory = Path(job.document_ref) if job.document_ref else None
    if spool_directory is None or not spool_directory.is_dir():
        raise FileNotFoundError(f"spool missing: {job.document_ref!r}")
    return sorted(path for path in spool_directory.iterdir() if path.is_file())


def _process_routed_job(
    job: Job,
    repository: SqliteRepository,
    fast_engine: OcrEngine,
    active_templates: list[dict],
    validation_context: ValidationContext,
) -> str:
    """A policy-deferred non-CI job: run the 2-lane router on the spooled document and
    FINALIZE the row (it was enqueued 'unrouted' — routing now says what it really is).
    Returns the terminal status."""
    source_paths = _spooled_files(job)
    routed = route_document(
        source_paths[0],
        TEMPLATES_DIRECTORY,
        fast_engine,
        category=job.category,
        templates=active_templates,
        context=validation_context,
        today=date.today(),
    )
    if routed.lane == "structured":
        status_value = _D1_STATUS_FOR_ROUTED_VERDICT.get(
            routed.verdict or "", STATUS_NEEDS_REVIEW
        )
        repository.update_status(
            job.job_id,
            status_value,
            verdict=routed.verdict,
            record_fields=routed.fields,
            reasons=[*job.reasons, *routed.reasons],
            category_lane="structured",
            category=routed.category,
            template_id=routed.template_id,
        )
    else:
        reasons = [
            *job.reasons,
            "non-structured document — routed to retrieval / human review",
        ]
        if routed.summary is not None and routed.summary.keywords:
            reasons.append("keywords: " + ", ".join(routed.summary.keywords))
        status_value = STATUS_NEEDS_REVIEW
        repository.update_status(
            job.job_id, status_value, reasons=reasons, category_lane="rag"
        )
    print(f"  job #{job.job_id}: routed -> {status_value}")
    return status_value


def _process_ci_job(
    job: Job,
    repository: SqliteRepository,
    fast_engine: OcrEngine,
    escalation_engine: OcrEngine,
) -> str:
    """Re-run the spooled CI submission WITH the escalation engine; write the terminal state.
    Returns the terminal status."""
    source_paths = _spooled_files(job)
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
    return status_value


def _process_claimed_job(
    job: Job,
    repository: SqliteRepository,
    fast_engine: OcrEngine,
    escalation_engine: OcrEngine,
    active_templates: list[dict],
    validation_context: ValidationContext,
) -> None:
    """Dispatch a claimed job to its flow (CI pair vs routed single doc).

    Spool policy: purged at every terminal state EXCEPT needs_review — a job awaiting a
    human keeps its bytes so the review page shows the document and the nightly draft
    pass can cluster the unknowns. The sweep purges it once the decision closes the job."""
    status_value = STATUS_FAILED
    try:
        if job.category_lane == "ci":
            status_value = _process_ci_job(
                job, repository, fast_engine, escalation_engine
            )
        else:
            status_value = _process_routed_job(
                job, repository, fast_engine, active_templates, validation_context
            )
    except Exception as error:  # terminal failure lands IN the row, never hidden
        repository.update_status(
            job.job_id,
            STATUS_FAILED,
            reasons=[f"worker failure: {type(error).__name__}: {error}"],
        )
        print(f"  job #{job.job_id}: FAILED ({type(error).__name__})")
    finally:
        if job.document_ref and status_value != STATUS_NEEDS_REVIEW:
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
        if job.document_ref:  # the decision closes the job -> its bytes leave the disk
            shutil.rmtree(job.document_ref, ignore_errors=True)
        closed += 1
    return closed


# The continuously running watchdog drains the doubt escalations AND the policy's
# async_immediate lane; 'nightly' rows wait for a `--nightly` pass (the night cron seam).
CONTINUOUS_EXECUTION_LANES = ("escalation", "deferred")


def _one_pass(
    repository: SqliteRepository,
    review_repository: SqliteReviewRepository,
    fast_engine: OcrEngine,
    escalation_engine: OcrEngine,
    active_templates: list[dict],
    validation_context: ValidationContext,
    lease_seconds: float,
    max_attempts: int,
    execution_lanes: tuple[str, ...] = CONTINUOUS_EXECUTION_LANES,
) -> int:
    """Recover -> drain each lane (all queued, one at a time) -> sweep. Returns jobs processed."""
    requeued, gave_up = repository.recover_stale(lease_seconds, max_attempts)
    if requeued or gave_up:
        print(f"  recover: {requeued} requeued, {gave_up} gave up (stale leases)")
    processed = 0
    for execution_lane in execution_lanes:
        while True:
            job = repository.claim_next(execution_lane)
            if job is None:
                break
            print(
                f"  claimed job #{job.job_id} (attempt {job.attempts}, "
                f"lane {execution_lane}) <- {job.source}"
            )
            _process_claimed_job(
                job,
                repository,
                fast_engine,
                escalation_engine,
                active_templates,
                validation_context,
            )
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
    parser.add_argument(
        "--nightly",
        action="store_true",
        help=(
            "Also drain the 'nightly' lane (execution policy async_nightly) AND run the "
            "DRAFT pass (cluster the accumulated unknowns -> draft templates -> stage "
            "D3 suggestions). The night cron runs `--once --nightly`; the continuous "
            "watchdog leaves both alone."
        ),
    )
    parser.add_argument(
        "--draft-ocr",
        action="store_true",
        help=(
            "Arm OCR for image-only unknowns in the DRAFT pass (default: skip them — "
            "the shared-machine brake, same contract as draft_check --ocr)."
        ),
    )
    parser.add_argument(
        "--slm-naming",
        action="store_true",
        help=(
            "Wake the SLM (llama-swap/granite) to name the drafts' placeholder fields "
            "(D-c part 1). Unreachable server degrades to placeholders, never blocks."
        ),
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
    # D2 read path for the routed (non-CI) jobs: same seed-then-read as the API door, so a
    # template promoted through the review page matches in the async lanes too.
    template_repository = SqliteTemplateRepository(arguments.store)
    template_repository.seed_from_directory(TEMPLATES_DIRECTORY)
    active_templates = template_repository.active_templates()
    issuer_registry_repository = SqliteIssuerRegistryRepository(arguments.store)
    fast_engine = _LazyRapidOcrEngine()
    escalation_engine = _build_escalation_engine(arguments.fake_escalation)
    execution_lanes = CONTINUOUS_EXECUTION_LANES + (
        ("nightly",) if arguments.nightly else ()
    )
    print(
        f"watchdog up (pid {os.getpid()}): store={arguments.store}, "
        f"escalation={escalation_engine.name}, lanes={'/'.join(execution_lanes)}, "
        f"{'once' if arguments.once else 'loop'}"
    )
    try:
        while True:
            # Rebuilt each pass: a registry edit through /registry applies to the very
            # next drained job. CI reference + validated attestations await the D-e
            # data decisions (document<->holder linkage) and stay None (fail-loud).
            validation_context = ValidationContext(
                issuer_registry=issuer_registry_repository.identifiers()
            )
            _one_pass(
                repository,
                review_repository,
                fast_engine,
                escalation_engine,
                active_templates,
                validation_context,
                arguments.lease_seconds,
                arguments.max_attempts,
                execution_lanes,
            )
            if arguments.nightly:
                # The DRAFT step of the night pass: unknowns that accumulated in D1
                # become staged template drafts the reviewer validates tomorrow.
                draft_report = run_draft_pass(
                    repository,
                    review_repository,
                    template_repository,
                    engine=fast_engine if arguments.draft_ocr else None,
                    slm_naming=arguments.slm_naming,
                )
                for staged_id in draft_report.staged_template_ids:
                    print(f"  draft pass: staged '{staged_id}' as a D3 suggestion")
                for skip_reason in draft_report.skipped:
                    print(f"  draft pass: skipped — {skip_reason}")
            if arguments.once:
                break
            time.sleep(arguments.interval)
    except KeyboardInterrupt:
        print("watchdog stopping (Ctrl+C).")
    finally:
        repository.close()
        review_repository.close()
        template_repository.close()
        issuer_registry_repository.close()
        arguments.pid_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
