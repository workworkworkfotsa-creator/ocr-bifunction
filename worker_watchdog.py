"""Watchdog worker — the SEPARATE process that owns every D1 status transition.

    uv run python worker_watchdog.py                 # loop (Ctrl+C to stop after current job)
    uv run python worker_watchdog.py --once          # one pass and exit (scheduler parity + smokes)
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
                the night scheduler runs `--once --nightly`. Terminal state written, spool dir
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
from dataclasses import replace
from datetime import date
from pathlib import Path

from ocr_bifunction.intake import handle_document
from ocr_bifunction.orchestrator import BatchItem
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
from ocr_bifunction.conformity_policy import SqliteConformityPolicyRepository
from ocr_bifunction.context_assembly import collect_validated_attestations
from ocr_bifunction.drafting_flow import run_draft_pass
from ocr_bifunction.issuer_registry import SqliteIssuerRegistryRepository
from ocr_bifunction.template import ORIGIN_HUMAN, ValidationContext, field_payload
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


def _spooled_files(job: Job) -> list[Path]:
    spool_directory = Path(job.document_ref) if job.document_ref else None
    if spool_directory is None or not spool_directory.is_dir():
        raise FileNotFoundError(f"spool missing: {job.document_ref!r}")
    return sorted(path for path in spool_directory.iterdir() if path.is_file())


def _process_claimed_job(
    job: Job,
    repository: SqliteRepository,
    fast_engine: OcrEngine,
    escalation_engine: OcrEngine,
    active_templates: list[dict],
    validation_context: ValidationContext,
    conformity_policies: dict,
) -> None:
    """Process a claimed job through the shared intake handler and write its terminal row.

    ONE path for both lanes (candidate B): a CI job (`category_lane == 'ci'`) re-runs the
    recto+verso submission WITH the escalation engine (the heavier VLM verso re-read); any
    other lane is a policy-deferred single document routed through the 2-lane router.
    `intake.handle_document` runs the pure core + the declared-vs-recognized type-mismatch
    check + the non-conformity reaction; this worker only PERSISTS the outcome (it owns the
    D1 status). Unlike the door there is no uploader to bounce an incomplete/unrecognized CI
    back to -> it lands in needs_review via the handler's general path (no done-trace edge).

    Spool policy: purged at every terminal state EXCEPT needs_review (a human reviews the
    doc next to its extraction) and rejected (the non-conforme evidence goes to the review /
    compliance). The sweep purges both once a decision lands."""
    status_value = STATUS_FAILED
    try:
        is_ci = job.category_lane == "ci"
        item = BatchItem(
            paths=_spooled_files(job),
            # CI MUST declare "carte_identite" so the handler dispatches the recto+verso
            # submission; a routed job carries its own declared category (may be None).
            document_type="carte_identite" if is_ci else job.category,
        )
        # The declared holder travels ON the row (manual entry at the door): it becomes this
        # job's reconcile_ci reference, layered over the pass-wide context (registry).
        job_context = replace(
            validation_context, ci_reference_name=job.expected_holder_name
        )
        outcome = handle_document(
            item,
            TEMPLATES_DIRECTORY,
            fast_engine,
            # Escalation (the heavy VLM verso re-read) is the CI lane's whole reason to be
            # here; a routed single doc never escalates.
            escalation_engine=escalation_engine if is_ci else None,
            templates=active_templates,
            context=job_context,
            today=date.today(),
            conformity_policies=conformity_policies,
        )
        reasons = [*job.reasons, *outcome.reasons]
        if is_ci and outcome.record.verso_read_path:
            # The escalation provenance (which verso read won) — a CI-only diagnostic with
            # no D1 column, folded into the reasons for the async follow-up (JobResponse).
            reasons.append(f"verso read via: {outcome.record.verso_read_path}")
        repository.update_status(
            job.job_id,
            outcome.status,
            verdict=outcome.verdict,
            record_fields=field_payload(outcome.record.fields),
            reasons=reasons,
            # A CI stays 'ci'; a routed 'unrouted' job is finalized to what it turned out to
            # be (structured/rag), or 'structured' when a type-mismatch reclassified it.
            category_lane=outcome.record.lane,
            category=outcome.record.category,
            template_id=outcome.record.template_id,
        )
        status_value = outcome.status
        print(f"  job #{job.job_id}: processed -> {status_value}")
    except Exception as error:  # terminal failure lands IN the row, never hidden
        repository.update_status(
            job.job_id,
            STATUS_FAILED,
            reasons=[f"worker failure: {type(error).__name__}: {error}"],
        )
        print(f"  job #{job.job_id}: FAILED ({type(error).__name__})")
    finally:
        if job.document_ref and status_value not in (
            STATUS_NEEDS_REVIEW,
            STATUS_REJECTED,
        ):
            shutil.rmtree(job.document_ref, ignore_errors=True)  # PII leaves the disk


def _apply_corrections(
    record_fields: dict[str, dict], corrections: dict[str, dict]
) -> dict[str, dict] | None:
    """The record with the human's edits applied — None when there is nothing to apply.

    A corrected value is authoritative, so it carries `origin: "human"` and NO spans: a typed
    value sits nowhere on the page, and pointing at the box the MACHINE read would show the
    reviewer a region that no longer holds what the field says. Absent provenance stays absent
    (the rule the whole provenance chain is built on).

    Returning None on an empty correction map keeps `update_status` from rewriting the column
    for the ordinary case, so an uncorrected accept is byte-identical to what it was before.
    """
    if not corrections:
        return None
    corrected = dict(record_fields)
    for name, correction in corrections.items():
        corrected[name] = {
            "value": correction.get("to"),
            "origin": ORIGIN_HUMAN,
            "spans": [],
        }
    return corrected


def _sweep_decisions(
    repository: SqliteRepository, review_repository: SqliteReviewRepository
) -> int:
    """Close D1 jobs whose review carries a human decision: accept -> done, reject -> failed.

    Idempotent by construction: only jobs still `needs_review` are touched, so a re-sweep of
    the same decision is a no-op. THIS process writes D1; the UI only wrote D3."""
    closed = 0
    for review in review_repository.decided():
        job = repository.get(review.job_id)
        if job is None:
            continue
        if job.status == STATUS_REJECTED:
            # A decided non-conformity (« clore » at the review): the evidence was
            # handed over — its bytes leave the disk. Status stays rejected (terminal);
            # purging only when the spool still exists keeps the re-sweep a no-op.
            if job.document_ref and Path(job.document_ref).is_dir():
                shutil.rmtree(job.document_ref, ignore_errors=True)
                print(
                    f"  job #{job.job_id}: non-conformity closed "
                    f"(review #{review.review_id}), evidence spool purged"
                )
            continue
        if job.status != STATUS_NEEDS_REVIEW:
            continue
        if review.decision == DECISION_ACCEPT:
            # Accepting is what makes a staged correction real: THIS process writes D1, so the
            # reviewer's edits (staged in D3) land in the record here and nowhere else. A
            # corrected value is authoritative and has NO geometry — nobody typed it onto the
            # page — so its provenance is honestly empty rather than the machine's old box.
            corrected_fields = _apply_corrections(
                job.record_fields, review.field_corrections
            )
            correction_reasons = [
                f"human corrected '{name}'" for name in sorted(review.field_corrections)
            ]
            repository.update_status(
                job.job_id,
                STATUS_DONE,
                record_fields=corrected_fields,
                reasons=[*job.reasons, "human decision: accept", *correction_reasons],
            )
            print(
                f"  job #{job.job_id}: closed done (human accepted, review #{review.review_id})"
                + (
                    f", {len(review.field_corrections)} field(s) corrected"
                    if review.field_corrections
                    else ""
                )
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
# async_immediate lane; 'nightly' rows wait for a `--nightly` pass (the night-scheduler seam).
CONTINUOUS_EXECUTION_LANES = ("escalation", "deferred")


def _one_pass(
    repository: SqliteRepository,
    review_repository: SqliteReviewRepository,
    fast_engine: OcrEngine,
    escalation_engine: OcrEngine,
    active_templates: list[dict],
    validation_context: ValidationContext,
    conformity_policies: dict,
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
                conformity_policies,
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
        "--once", action="store_true", help="One pass and exit (scheduler parity)."
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
            "D3 suggestions). The night scheduler runs `--once --nightly`; the continuous "
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
    conformity_policy_repository = SqliteConformityPolicyRepository(arguments.store)
    conformity_policy_repository.seed_defaults()
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
            # Rebuilt each pass: a registry edit or a freshly closed attestation
            # applies to the very next drained job. The CI reference is per-job (the
            # declared holder on the row); attestations project through each
            # template's métier-configured roles block.
            validation_context = ValidationContext(
                issuer_registry=issuer_registry_repository.identifiers(),
                validated_attestations=collect_validated_attestations(
                    repository, active_templates
                ),
            )
            conformity_policies = {
                policy.category: policy
                for policy in conformity_policy_repository.all_policies()
            }
            _one_pass(
                repository,
                review_repository,
                fast_engine,
                escalation_engine,
                active_templates,
                validation_context,
                conformity_policies,
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
        conformity_policy_repository.close()
        arguments.pid_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
