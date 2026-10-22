"""`raw-uris migrate` (scanner.runtime.raw_uris) on a migrated database. Run with TEST_DATABASE_URL set.

The test's rows (source test_rawuris) are written and migrated inside one transaction that is rolled back,
so references already in the database are never changed; the assertions look at the test's rows only.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from scanner.core.raw_store import RawStore
from scanner.runtime.raw_uris import migrate

pytestmark = pytest.mark.db

HOST = "file://C:/Users/x/grid-scanner/.data/raw/raw/test_rawuris/ds/2026/10/18/{}-0.json"
CONTAINER = "file:///data/raw/raw/test_rawuris/ds/2026/10/18/{}-0.json"
RELATIVE = "raw://test_rawuris/ds/2026/10/18/{}-0.json"


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    c = psycopg.connect(url)
    yield c
    c.rollback()
    c.close()


def _rows(conn: psycopg.Connection[Any], run_id: uuid.UUID, doc: str) -> tuple[Any, ...]:
    row = conn.execute(
        "select (select raw_uris from ops.ingest_run where run_id = %(run)s), "
        "(select array_agg(raw_uri order by outbox_id) from ops.bead_outbox where source = 'test_rawuris'), "
        "(select raw_uri from ops.snapshot where snapshot_id = 'test_rawuris'), "
        "(select raw_uri from core.source_document where document_id = %(doc)s)",
        {"run": run_id, "doc": doc},
    ).fetchone()
    assert row is not None
    return tuple(row)


def test_absolute_references_become_relative_once(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    run_id, doc = uuid.uuid4(), "test_rawuris:doc"
    conn.execute(
        "insert into ops.ingest_run (run_id, source, dataset, status, raw_uris) values (%s, 'test_rawuris', 'ds', "
        "'ok', %s)",
        (run_id, [HOST.format("a"), CONTAINER.format("b"), RELATIVE.format("c")]),
    )
    conn.execute(
        "insert into ops.bead_outbox (kind, title, body, raw_uri, source) values "
        "('adapter-fix', 't', 'b', %s, 'test_rawuris'), ('human', 't', 'b', null, 'test_rawuris'), "
        "('research-source', 't', 'b', 's3://.../raw/...', 'test_rawuris')",
        (HOST.format("a"),),
    )
    conn.execute(
        "insert into ops.snapshot (snapshot_id, url, raw_uri, changed) values ('test_rawuris', 'u', %s, false)",
        (CONTAINER.format("d"),),
    )
    conn.execute(
        "insert into core.source_document (document_id, source, fetched_at, sha256, raw_uri, content_type) "
        "values (%s, 'test_rawuris', %s, 'x', %s, 'application/json')",
        (doc, datetime(2026, 10, 18, tzinfo=UTC), HOST.format("e")),
    )
    before = _rows(conn, run_id, doc)

    dry = migrate(conn, apply=False)
    assert _rows(conn, run_id, doc) == before  # a dry run changes nothing
    assert {r.column for r in dry} == {
        "ops.ingest_run.raw_uris",
        "ops.bead_outbox.raw_uri",
        "ops.snapshot.raw_uri",
        "core.source_document.raw_uri",
    }

    store = RawStore(tmp_path.as_uri())
    store.put(
        source="test_rawuris", dataset="ds", run_id="a", seq=0, content=b"x", ext="json",
        fetched_at=datetime(2026, 10, 18, 12, tzinfo=UTC), meta={},
    )  # fmt: skip
    reports = {r.column: r for r in migrate(conn, apply=True, verify=store)}
    assert _rows(conn, run_id, doc) == (
        [RELATIVE.format("a"), RELATIVE.format("b"), RELATIVE.format("c")],
        [RELATIVE.format("a"), None, "s3://.../raw/..."],  # not in the store layout: kept
        RELATIVE.format("d"),
        RELATIVE.format("e"),
    )
    runs = reports["ops.ingest_run.raw_uris"]
    assert runs.roots["file://C:/Users/x/grid-scanner/.data/raw"] >= 1 and runs.roots["file:///data/raw"] >= 1
    assert RELATIVE.format("b") in runs.missing and RELATIVE.format("a") not in runs.missing
    assert "s3://.../raw/..." in reports["ops.bead_outbox.raw_uri"].unrecognised

    again = migrate(conn, apply=True)
    assert sum(r.rewritten for r in again) == 0  # idempotent
    assert _rows(conn, run_id, doc)[0] == [RELATIVE.format(x) for x in "abc"]
