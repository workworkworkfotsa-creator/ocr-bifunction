"""Resilient-conversion smoke — the split / decreasing-backoff / reconcile core
(resilient_conversion.py).

    uv run python resilient_conversion_smoke.py

Runs NO Docling (the shared machine is preserved). A deterministic FAKE converter models the
real failure — memory accumulation makes a big batch DROP pages that a fresh SMALLER batch
produces impeccably — so the whole split -> retry-on-smaller -> reconcile algorithm is proven as
a pure unit, exactly as the real 21/50-page documents behaved. Proves:
  1. full success first try (round-0 multi-chunk partition);
  2. a mid-document drop is recovered on a later, smaller pass;
  3. provenance: a recovered page is tagged with the batch width that produced it;
  4. a page that never recovers -> named missing -> routed to review (never a silent success);
  5. the guard gate invariant (assessment.missing == condemned; produced u missing = all, disjoint);
  6. disjoint-union correctness (each page once, ascending, right content);
  7. a low-form page is reported but NEVER retried (form is content, not a batch-size problem);
  8. no redundant work (a retry pass touches only previously-missing pages);
  9. a single-page document (clean, and genuinely-bad);
 10. a whole-chunk FAILURE status is recovered on the smaller pass and surfaced for observability;
 11. the backoff schedule levier fails LOUD when malformed.
"""

from __future__ import annotations

from ocr_bifunction.resilient_conversion import (
    CONVERSION_STATUS_FAILURE,
    CONVERSION_STATUS_PARTIAL_SUCCESS,
    CONVERSION_STATUS_SUCCESS,
    PageRangeConversionAttempt,
    reconcile_page_range_conversion,
)

CHECKS: list[tuple[str, bool]] = []


def _check(label: str, condition: bool) -> None:
    CHECKS.append((label, condition))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")


class FakeMemoryBoundedConverter:
    """A converter whose failures model real memory contention, not content.

    A `fragile` page is DROPPED whenever the attempted chunk is wider than
    `drop_threshold_batch_size` (the batch accumulated too much memory) and PRODUCED once the
    chunk is small enough (a fresh, light conversion). An `always_bad` page never comes out at any
    width (genuinely bad content). A `low_form` page comes out but with a poor layout score. A
    `whole_failure` range returns zero pages with a FAILURE status (the whole chunk crashed). Every
    requested range is recorded so the smoke can prove no page is retried needlessly."""

    def __init__(
        self,
        *,
        drop_threshold_batch_size: int = 0,
        fragile_page_numbers: frozenset[int] = frozenset(),
        always_bad_page_numbers: frozenset[int] = frozenset(),
        low_form_page_numbers: frozenset[int] = frozenset(),
        whole_failure_ranges: frozenset[tuple[int, int]] = frozenset(),
    ) -> None:
        self.drop_threshold_batch_size = drop_threshold_batch_size
        self.fragile_page_numbers = set(fragile_page_numbers)
        self.always_bad_page_numbers = set(always_bad_page_numbers)
        self.low_form_page_numbers = set(low_form_page_numbers)
        self.whole_failure_ranges = set(whole_failure_ranges)
        self.requested_ranges: list[tuple[int, int]] = []

    def __call__(self, page_range: tuple[int, int]) -> PageRangeConversionAttempt:
        self.requested_ranges.append(page_range)
        range_start, range_end = page_range
        chunk_width = range_end - range_start + 1
        expected_page_numbers = list(range(range_start, range_end + 1))

        if page_range in self.whole_failure_ranges:
            return PageRangeConversionAttempt(
                requested_page_range=page_range,
                produced_page_numbers=[],
                page_markdown={},
                page_layout_scores={},
                status=CONVERSION_STATUS_FAILURE,
            )

        produced_page_numbers: list[int] = []
        for page_number in expected_page_numbers:
            if page_number in self.always_bad_page_numbers:
                continue
            if (
                page_number in self.fragile_page_numbers
                and chunk_width > self.drop_threshold_batch_size
            ):
                continue
            produced_page_numbers.append(page_number)

        page_markdown = {
            page_number: f"# Page {page_number}\n\nContent of page {page_number}."
            for page_number in produced_page_numbers
        }
        page_layout_scores = {
            page_number: (0.6 if page_number in self.low_form_page_numbers else 0.95)
            for page_number in produced_page_numbers
        }
        if not produced_page_numbers:
            status = CONVERSION_STATUS_FAILURE
        elif len(produced_page_numbers) == len(expected_page_numbers):
            status = CONVERSION_STATUS_SUCCESS
        else:
            status = CONVERSION_STATUS_PARTIAL_SUCCESS
        return PageRangeConversionAttempt(
            requested_page_range=page_range,
            produced_page_numbers=produced_page_numbers,
            page_markdown=page_markdown,
            page_layout_scores=page_layout_scores,
            status=status,
        )


def run() -> int:
    # 1. Full success on the first pass; round 0 partitions 8 pages into two width-4 chunks.
    clean = FakeMemoryBoundedConverter()
    clean_result = reconcile_page_range_conversion(
        8, clean, batch_size_schedule=[4, 2, 1]
    )
    _check(
        "full success first try -> complete, ordered 1..8, provenance 4, all round 0",
        clean_result.complete
        and not clean_result.missing_page_numbers
        and [page.page_number for page in clean_result.page_results]
        == list(range(1, 9))
        and all(page.produced_by_batch_size == 4 for page in clean_result.page_results)
        and all(page.produced_in_round == 0 for page in clean_result.page_results),
    )

    # 2. A mid-document drop (pages 5, 6 die at width 10) recovers on the smaller pass.
    fragile = FakeMemoryBoundedConverter(
        drop_threshold_batch_size=4, fragile_page_numbers=frozenset({5, 6})
    )
    fragile_result = reconcile_page_range_conversion(
        10, fragile, batch_size_schedule=[10, 5, 2, 1]
    )
    produced_fragile = {page.page_number for page in fragile_result.page_results}
    _check(
        "mid-doc drop recovers on the smaller pass -> complete, all 10 pages present",
        fragile_result.complete
        and not fragile_result.missing_page_numbers
        and produced_fragile == set(range(1, 11)),
    )

    # 3. Provenance: recovered pages carry the width AND the retry ROUND that produced them
    #    (round 1, the batch-10 pass); a clean page stays round 0 at width 10. The round is the
    #    honest "backoff saved it" signal — a round-0 remainder chunk is narrow yet NOT a recovery.
    by_page = {page.page_number: page for page in fragile_result.page_results}
    _check(
        "provenance: recovered pages 5,6 -> width 2 / round 1; clean page 1 -> width 10 / round 0",
        by_page[5].produced_by_batch_size == 2
        and by_page[6].produced_by_batch_size == 2
        and by_page[5].produced_in_round == 1
        and by_page[6].produced_in_round == 1
        and by_page[1].produced_by_batch_size == 10
        and by_page[1].produced_in_round == 0,
    )

    # 4. A page that never recovers is named missing and routed to review, never silently dropped.
    bad = FakeMemoryBoundedConverter(always_bad_page_numbers=frozenset({9}))
    bad_result = reconcile_page_range_conversion(
        10, bad, batch_size_schedule=[10, 5, 2, 1]
    )
    _check(
        "never-recovered page -> missing [9], not complete, reason names it",
        not bad_result.complete
        and bad_result.missing_page_numbers == [9]
        and bad_result.assessment.missing_pages == [9]
        and any("9" in reason for reason in bad_result.assessment.reasons),
    )

    # 5. The guard gate is the single source of truth: assessment == condemned, and the two sets
    #    partition the document exactly (produced u missing = all, disjoint).
    produced_bad = {page.page_number for page in bad_result.page_results}
    _check(
        "guard-gate invariant: assessment.missing == condemned; produced u missing = all, disjoint",
        bad_result.assessment.missing_pages == sorted(bad_result.missing_page_numbers)
        and produced_bad | set(bad_result.missing_page_numbers) == set(range(1, 11))
        and not (produced_bad & set(bad_result.missing_page_numbers)),
    )

    # 6. Disjoint union: each page once, strictly ascending, carrying its own page's content.
    page_numbers = [page.page_number for page in fragile_result.page_results]
    _check(
        "disjoint union: unique, ascending, per-page markdown correct",
        page_numbers == sorted(set(page_numbers))
        and page_numbers == list(range(1, 11))
        and all(
            page.markdown
            == f"# Page {page.page_number}\n\nContent of page {page.page_number}."
            for page in fragile_result.page_results
        ),
    )

    # 7. A low-form page surfaces on its own signal but is NEVER retried (only one attempt made).
    low_form = FakeMemoryBoundedConverter(low_form_page_numbers=frozenset({2}))
    low_form_result = reconcile_page_range_conversion(
        6, low_form, batch_size_schedule=[6, 3, 1]
    )
    _check(
        "low-form page reported but NOT retried (present, complete, single attempt)",
        low_form_result.complete
        and low_form_result.low_form_page_numbers == [2]
        and 2 in {page.page_number for page in low_form_result.page_results}
        and low_form.requested_ranges == [(1, 6)],
    )

    # 8. No redundant work: every retry pass touches ONLY the pages that were still missing.
    retry_ranges = fragile.requested_ranges[1:]
    _check(
        "no redundant work: retry passes touch only the previously-missing pages {5,6}",
        all(set(range(start, end + 1)) <= {5, 6} for start, end in retry_ranges),
    )

    # 9. Single-page document: clean -> complete; genuinely bad -> missing, and it TERMINATES.
    single_clean = reconcile_page_range_conversion(
        1, FakeMemoryBoundedConverter(), batch_size_schedule=[1]
    )
    single_bad = reconcile_page_range_conversion(
        1,
        FakeMemoryBoundedConverter(always_bad_page_numbers=frozenset({1})),
        batch_size_schedule=[1],
    )
    _check(
        "single-page doc: clean -> complete width 1; bad -> missing [1], no infinite loop",
        single_clean.complete
        and len(single_clean.page_results) == 1
        and single_clean.page_results[0].produced_by_batch_size == 1
        and not single_bad.complete
        and single_bad.missing_page_numbers == [1],
    )

    # 10. A whole chunk that fails wholesale (FAILURE status) is recovered on the smaller pass and
    #     the noisy status is surfaced for observability without blocking completeness.
    whole_failure = FakeMemoryBoundedConverter(whole_failure_ranges=frozenset({(1, 4)}))
    whole_failure_result = reconcile_page_range_conversion(
        4, whole_failure, batch_size_schedule=[4, 2, 1]
    )
    _check(
        "whole-chunk FAILURE recovers on smaller pass (round 1); status surfaced in observability",
        whole_failure_result.complete
        and all(
            page.produced_by_batch_size == 2 and page.produced_in_round == 1
            for page in whole_failure_result.page_results
        )
        and ((1, 4), CONVERSION_STATUS_FAILURE)
        in whole_failure_result.nonclean_chunk_statuses,
    )

    # 11. The backoff schedule is a levier that must fail LOUD when malformed.
    malformed_schedules = [[], [4, 2], [2, 3, 1], [-1, 1], [3, 1, 2]]
    all_rejected = True
    for schedule in malformed_schedules:
        try:
            reconcile_page_range_conversion(
                4, FakeMemoryBoundedConverter(), batch_size_schedule=schedule
            )
            all_rejected = False
        except ValueError:
            pass
    _check("malformed batch_size_schedule -> ValueError (fail loud)", all_rejected)

    passed = all(condition for _, condition in CHECKS)
    print(
        f"\nEXPECT resilient conversion (split + decreasing backoff + reconcile): "
        f"{'PASS' if passed else 'FAIL'} "
        f"({sum(condition for _, condition in CHECKS)}/{len(CHECKS)})"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(run())
