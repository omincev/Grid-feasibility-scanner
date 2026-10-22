"""serving.source_completeness (docs/ui-contract.md): gaps inside a dataset's history, from ops.ingest_run.

Run with TEST_DATABASE_URL set. The test's datasets (source test_completeness) and runs are written in one
transaction that is rolled back; the assertions read the test's rows only, so data already in the database
never enters them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.db

SRC = "test_completeness"


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    c = psycopg.connect(url)
    yield c
    c.rollback()
    c.close()


def _dataset(conn: psycopg.Connection[Any], dataset: str, start: date, end: date | None, sensitivity: str) -> None:
    conn.execute(
        "insert into ref.dataset (source, dataset, kind, country_code, status, history_start, history_end, "
        "sensitivity, source_path) values (%s, %s, 'manifest', 'dk', 'active', %s, %s, %s, 'test')",
        (SRC, dataset, start, end, sensitivity),
    )


def _run(
    conn: psycopg.Connection[Any], dataset: str, day: date, status: str, received: int | None, expected: int | None
) -> None:
    conn.execute(
        "insert into ops.ingest_run (run_id, source, dataset, partition_key, status, rows_received, rows_expected) "
        "values (%s, %s, %s, %s, %s, %s, %s)",
        (uuid.uuid4(), SRC, dataset, day.isoformat(), status, received, expected),
    )


def test_counts_complete_short_unchecked_and_missing_days(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
        "values ('dk', 'Denmark', 'active', 'Europe/Copenhagen', 'DKK', '{}', 't', 't') on conflict do nothing"
    )
    row = conn.execute("select (now() at time zone 'Europe/Copenhagen')::date").fetchone()
    assert row is not None
    today: date = row[0]
    d = [today - timedelta(days=n) for n in range(7, 0, -1)]  # the seven days before today, oldest first
    _dataset(conn, "Live", d[0], None, "public")
    _run(conn, "Live", d[0], "ok", 96, 96)  # complete
    _run(conn, "Live", d[1], "ok", 90, 96)  # short at first, complete on a later run
    _run(conn, "Live", d[1], "ok", 96, 96)
    _run(conn, "Live", d[2], "ok", 95, 96)  # incomplete: every checked run short
    _run(conn, "Live", d[2], "ok", 93, 96)
    _run(conn, "Live", d[3], "ok", 96, None)  # unchecked: run on the day itself
    _run(conn, "Live", d[4], "blocked", 0, 96)  # missing: no successful run
    _run(conn, "Live", d[5], "ok", 92, 92)  # complete (a short DST day)
    # d[6] has no run at all: missing. Today is not counted, whatever its runs say.
    _run(conn, "Live", today, "ok", 10, 96)
    # A retired dataset ends at its history end; a licensed dataset is not served.
    _dataset(conn, "Retired", d[0], d[1], "public")
    _run(conn, "Retired", d[0], "ok", 24, 24)
    _run(conn, "Retired", d[1], "ok", 24, 24)
    _run(conn, "Retired", d[2], "ok", 1, 24)  # after the history end: ignored
    _dataset(conn, "Hidden", d[0], None, "licensed")

    conn.execute("set local role anon")
    cur = conn.execute("select * from serving.source_completeness where source = %s order by dataset", (SRC,))
    names = [c.name for c in cur.description or []]
    rows = {r["dataset"]: r for r in (dict(zip(names, v, strict=True)) for v in cur.fetchall())}
    assert set(rows) == {"Live", "Retired"}
    live = rows["Live"]
    assert (live["first_partition"], live["last_partition"]) == (d[0], d[6])
    counts = ("expected", "complete", "incomplete", "unchecked", "missing")
    assert [live[f"partitions_{c}"] for c in counts] == [7, 3, 1, 1, 2]
    assert (live["rows_expected"], live["rows_received"]) == (96 * 3 + 92, 96 * 2 + 95 + 92)
    assert live["incomplete_partitions"] == [d[2].isoformat()]
    assert live["missing_partitions"] == [d[4].isoformat(), d[6].isoformat()]
    assert live["complete"] is False

    retired = rows["Retired"]
    assert (retired["last_partition"], retired["partitions_expected"], retired["complete"]) == (d[1], 2, True)
    assert retired["incomplete_partitions"] == [] and retired["missing_partitions"] == []


def test_unchecked_days_leave_completeness_unknown(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
        "values ('dk', 'Denmark', 'active', 'Europe/Copenhagen', 'DKK', '{}', 't', 't') on conflict do nothing"
    )
    row = conn.execute("select (now() at time zone 'Europe/Copenhagen')::date - 2").fetchone()
    assert row is not None
    first: date = row[0]
    _dataset(conn, "Daily", first, None, "public")
    _run(conn, "Daily", first, "ok", 1, 1)
    _run(conn, "Daily", first + timedelta(days=1), "ok", 1, None)
    complete = conn.execute(
        "select partitions_unchecked, complete from serving.source_completeness where source = %s", (SRC,)
    ).fetchone()
    assert complete == (1, None)
