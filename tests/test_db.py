"""Database contract tests. Run with: TEST_DATABASE_URL=... pytest -m db

Requires migrations applied (scripts/migrate_local.py or dbmate). Creates minimal reference rows.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from scanner.core import db
from scanner.core.sensitivity import Sensitivity
from scanner.runtime.provenance import compute_run

pytestmark = pytest.mark.db

_T0 = datetime(2025, 10, 1, tzinfo=UTC)

SENSITIVITY_EXEMPT = {
    ("ref", "country"),
    ("ref", "entity_alias"),
    ("ref", "source"),
    ("ref", "equivalence_class"),
    ("ref", "market_rule"),
    ("core", "observation"),
}


@pytest.fixture()
def conn():  # type: ignore[no-untyped-def]
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    c = psycopg.connect(url)
    yield c
    c.rollback()
    c.close()


def _setup_series(conn: psycopg.Connection, key: str = "test.series") -> int:  # type: ignore[type-arg]
    conn.execute(
        "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
        "values ('dk','Denmark','active','Europe/Copenhagen','DKK','{}','t','t') on conflict do nothing"
    )
    conn.execute(
        "insert into ref.entity (entity_id, kind, country_code, name) values ('dk.zone.dk1','zone','dk','DK1') "
        "on conflict do nothing"
    )
    conn.execute(
        "insert into ref.equivalence_class values ('day_ahead_price','currency_per_energy','x') on conflict do nothing"
    )
    conn.execute(
        "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, equivalence_class, kind, unit, "
        "resample, definition, status, source_path, content_hash) values ('dk.eds.T.P','eds','T','dk','P',"
        "'day_ahead_price','actual','EUR/MWh','mean','{}','draft','t','t') on conflict do nothing"
    )
    return db.ensure_series(
        conn,
        db.SeriesSpec(
            key,
            "dk.eds.T.P",
            "dk.zone.dk1",
            "EUR/MWh",
            timedelta(minutes=15),
            "actual",
            "public",
            None,
        ),
    )


def test_append_only_revisions_and_asof(conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
    # Its own series, removed afterwards: the commits below would otherwise leave versions that a later
    # run of this test reads as its own.
    key = f"test.series.{uuid.uuid4().hex[:8]}"
    sid = _setup_series(conn, key)
    t0 = datetime(2025, 10, 1, tzinfo=UTC)
    try:
        rows = [db.ObservationRow(key, t0, t0 + timedelta(minutes=15), None, 10.0)]
        assert db.write_observations(conn, rows, {key: sid}, uuid.uuid4()) == 1
        assert db.write_observations(conn, rows, {key: sid}, uuid.uuid4()) == 0  # unchanged: no write
        conn.execute("select pg_sleep(0.01)")
        conn.commit()
        revised = [db.ObservationRow(key, t0, t0 + timedelta(minutes=15), None, 12.0)]
        assert db.write_observations(conn, revised, {key: sid}, uuid.uuid4()) == 1
        latest = conn.execute("select value from core.observation_latest where series_id = %s", (sid,)).fetchall()
        assert latest == [(12.0,)]
        first = conn.execute("select min(ingested_at) from core.observation where series_id = %s", (sid,)).fetchone()
        assert first is not None
        asof = conn.execute("select value from core.observation_asof(%s) where series_id = %s", (first[0], sid))
        assert asof.fetchall() == [(10.0,)]
    finally:
        _drop_series(conn, sid)


def _drop_series(conn: psycopg.Connection, sid: int) -> None:  # type: ignore[type-arg]
    """Remove a committed test series, its rows and the test's codebook entry once no series uses it."""
    conn.rollback()
    conn.execute("delete from core.observation where series_id = %s", (sid,))
    conn.execute("delete from core.series where series_id = %s", (sid,))
    conn.execute(
        "delete from ref.codebook_entry where entry_id = 'dk.eds.T.P' and source_path = 't' "
        "and not exists (select 1 from core.series where entry_id = 'dk.eds.T.P')"
    )
    conn.commit()


def test_series_takes_a_raised_codebook_sensitivity(conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
    """ADR 0008: a codebook entry raised from public to licensed must not keep feeding a public series (the
    serving views and features read core.series.sensitivity). Lowering is left to a person."""
    sid = _setup_series(conn, "test.sensitivity")

    def ensure(sensitivity: Sensitivity) -> tuple[int, str]:
        q = timedelta(minutes=15)
        got = db.ensure_series(
            conn,
            db.SeriesSpec("test.sensitivity", "dk.eds.T.P", "dk.zone.dk1", "EUR/MWh", q, "actual", sensitivity, None),
        )
        row = conn.execute("select sensitivity::text from core.series where series_id = %s", (got,)).fetchone()
        assert row is not None
        return got, row[0]

    assert ensure("licensed") == (sid, "licensed")
    assert ensure("public") == (sid, "licensed")


def test_quarantine_label_is_added_but_never_cleared(conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
    """docs/quality.md layer 2: an unchanged value gains a quarantined version over an ok one, never the
    reverse; a changed value is written with the quality it carries."""
    key = f"test.label.{uuid.uuid4().hex[:8]}"
    sid = _setup_series(conn, key)
    t0 = datetime(2026, 2, 17, 16, 45, tzinfo=UTC)

    def write(value: float, quality: str) -> tuple[int, uuid.UUID]:
        run_id = uuid.uuid4()
        row = db.ObservationRow(key, t0, t0 + timedelta(minutes=15), None, value, quality)
        n = db.write_observations(conn, [row], {key: sid}, run_id)
        conn.commit()  # one transaction per run, as in run_ingest, so each version has its own ingested_at
        return n, run_id

    def latest() -> tuple[float, str, uuid.UUID]:
        row = conn.execute(
            "select value, quality, run_id from core.observation_latest where series_id = %s", (sid,)
        ).fetchone()
        assert row is not None
        return row[0], row[1], row[2]

    try:
        n, ok_run = write(911.14, "ok")
        assert n == 1 and latest() == (911.14, "ok", ok_run)
        n, soft_run = write(911.14, "quarantined")  # same value, ok -> quarantined: a new version
        assert n == 1 and latest() == (911.14, "quarantined", soft_run)
        assert write(911.14, "quarantined")[0] == 0  # already quarantined: the first label's run keeps it
        assert write(911.14, "ok")[0] == 0  # a clean run does not clear the label
        assert latest() == (911.14, "quarantined", soft_run)
        n, revised_run = write(900.0, "ok")  # a revised value is always written, with its own quality
        assert n == 1 and latest() == (900.0, "ok", revised_run)
        n, revised_soft = write(901.0, "quarantined")
        assert n == 1 and latest() == (901.0, "quarantined", revised_soft)
        versions = conn.execute("select count(*) from core.observation where series_id = %s", (sid,)).fetchone()
        assert versions == (4,)  # append-only (ADR 0003)
    finally:
        _drop_series(conn, sid)


def test_runs_record_when_they_finished_not_when_their_last_transaction_began(
    conn: psycopg.Connection,  # type: ignore[type-arg]
) -> None:
    """finished_at is written inside a transaction that began before the run's work (now() is its start)."""
    run_id = db.start_run(
        conn, source="test_timing", dataset="T", partition_key=None, window_start=_T0, window_end=_T0, code_version=None
    )
    try:
        conn.execute("select pg_sleep(0.2)")  # the run's work, in the transaction finish_run then commits
        db.finish_run(conn, run_id, status="ok", rows_received=0, rows_expected=None, rows_written=0, raw_uris=[])
        with compute_run(conn, kind="feature", name="test_timing", params={"test": "timing"}) as run:
            conn.execute("select pg_sleep(0.2)")
        durations = [
            conn.execute(f"select finished_at - started_at from ops.{table} where run_id = %s", (rid,)).fetchone()
            for table, rid in (("ingest_run", run_id), ("compute_run", run.run_id))
        ]
        assert all(d is not None and d[0] >= timedelta(seconds=0.2) for d in durations), durations
    finally:
        conn.rollback()
        conn.execute("delete from ops.compute_run where params->>'test' = 'timing'")
        conn.execute("delete from ops.ingest_run where source = 'test_timing'")
        conn.commit()


def test_every_data_table_has_sensitivity_and_rls(conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
    tables = conn.execute(
        "select table_schema, table_name from information_schema.tables "
        "where table_schema in ('ref','core','feat','val') and table_type = 'BASE TABLE'"
    ).fetchall()
    with_col = set(
        conn.execute(
            "select table_schema, table_name from information_schema.columns where column_name = 'sensitivity'"
        ).fetchall()
    )
    rls = set(
        conn.execute(
            "select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace where c.relrowsecurity"
        ).fetchall()
    )
    missing_col = [t for t in tables if t not in with_col and t not in SENSITIVITY_EXEMPT]
    missing_rls = [t for t in tables if t in with_col and t not in rls]
    assert not missing_col, f"tables without sensitivity column: {missing_col}"
    assert not missing_rls, f"tables with sensitivity but no RLS: {missing_rls}"


def test_anon_cannot_read_core(conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
    conn.execute("set local role anon")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("select * from core.observation limit 1")
