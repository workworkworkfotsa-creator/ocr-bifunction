"""Store smoke — the shared connection + in-memory seam (Candidate D).

Proves the consolidation works WITHOUT touching disk or a subprocess: one Store(":memory:")
shared by several repos holds all their tables in ONE database, round-trips a record, applies a
migration, and — the load-bearing fact — two separate Store(":memory:") are INDEPENDENT, which
is exactly why the repos must share one connection for an in-memory store to hold every table
together. Runs in milliseconds; the cheap unit-test layer the repos never had.
"""

from __future__ import annotations

from ocr_bifunction.governance.capacity_settings import SqliteCapacitySettingsRepository
from ocr_bifunction.storage.repository import Job, SqliteRepository
from ocr_bifunction.storage.review_repository import Review, SqliteReviewRepository
from ocr_bifunction.validation.status import STATUS_NEEDS_REVIEW
from ocr_bifunction.storage.store import Store
from ocr_bifunction.storage.template_repository import SqliteTemplateRepository

_checks_passed = 0


def _check(label: str, condition: bool) -> None:
    global _checks_passed
    print(f"  {'PASS' if condition else 'FAIL'} {label}")
    assert condition, label
    _checks_passed += 1


def main() -> None:
    print("=== store: one shared in-memory connection ===")

    # Four repos on ONE Store(":memory:") — they must all see the same database.
    store = Store(":memory:")
    jobs = SqliteRepository(store)
    reviews = SqliteReviewRepository(store)
    SqliteTemplateRepository(store)  # creates ocr_templates on the shared connection
    capacity = SqliteCapacitySettingsRepository(store)

    tables = {
        row["name"]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    _check(
        "all repos' tables coexist on the one shared connection",
        {"ocr_jobs", "ocr_reviews", "ocr_templates", "ocr_capacity_settings"} <= tables,
    )

    # Round-trip a D1 record through the in-memory store.
    job_id = jobs.save(
        Job(
            source="doc.pdf",
            category_lane="structured",
            status=STATUS_NEEDS_REVIEW,
            expected_holder_name="FICTIF Alice",
        )
    )
    fetched = jobs.get(job_id)
    _check(
        "save -> get round-trips the record (in-memory, no disk)",
        fetched is not None
        and fetched.source == "doc.pdf"
        and fetched.expected_holder_name == "FICTIF Alice",
    )

    # Cross-repo sharing: a review opened on one repo is visible to a NEW repo on the SAME
    # store — the connection, not the object, holds the data.
    review_id = reviews.open_review(
        Review(job_id=job_id, projection={"source": "doc.pdf"})
    )
    reviews_again = SqliteReviewRepository(store)
    _check(
        "a second repo on the same store sees the first's writes",
        reviews_again.by_job(job_id) is not None
        and reviews_again.by_job(job_id).review_id == review_id,
    )

    # Capacity levers seed + read through the shared store.
    capacity.seed_defaults()
    _check(
        "capacity defaults seed and read back on the shared store",
        capacity.get("SYNC_CONCURRENCY_LIMIT") == "2",
    )

    print("\n=== isolation: separate :memory: stores are independent ===")
    other = Store(":memory:")
    other_jobs = SqliteRepository(other)
    _check(
        "a separate Store(':memory:') does NOT see the first store's row "
        "(why the repos must SHARE one connection)",
        other_jobs.get(job_id) is None,
    )

    print("\n=== migration mechanism (Store.ensure_schema) ===")
    migrated = Store(":memory:")
    migrated.connection.execute("CREATE TABLE demo (a TEXT)")
    migrated.connection.commit()
    migrated.ensure_schema(
        "CREATE TABLE IF NOT EXISTS demo (a TEXT)",
        table="demo",
        migrations={"b": "ALTER TABLE demo ADD COLUMN b TEXT"},
    )
    columns = {
        row["name"] for row in migrated.connection.execute("PRAGMA table_info(demo)")
    }
    _check("ensure_schema adds a missing column on an existing table", "b" in columns)

    print("\n=== path-or-Store accept (back-compat) ===")
    wrapped = SqliteRepository(":memory:")  # a bare path/name wraps its OWN Store
    _check(
        "a repo given a path wraps its own store (isolated from `store`)",
        wrapped.get(job_id) is None,
    )

    store.close()
    other.close()
    migrated.close()
    wrapped.close()
    print(f"\nSTORE SMOKE PASS {_checks_passed}/{_checks_passed}")


if __name__ == "__main__":
    main()
