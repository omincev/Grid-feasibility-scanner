"""The analytics database role (migration 20261018130000). Run with: TEST_DATABASE_URL=... pytest -m db

scanner_analytics reads ref, core, ops, feat and val, writes only feat, val and ops.compute_run, and may switch
to anon for the serving views. Real feature, score and valuation runs as a login role in it are tested in
test_features_db.py and test_scores_db.py.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.db

READ = ["ref.scenario", "ref.assumption_set", "ref.entity", "core.series", "core.observation", "ops.ingest_run"]
READ += ["ops.compute_run", "feat.feature_value", "feat.score", "val.run", "val.result"]
DENIED_WRITES = [
    "insert into ref.scenario (scenario_id, title, status, description, owners, sets, source_path, content_hash) "
    "values ('test_analytics_role', 't', 'exploratory', 't', '{x}', '{}', 't', 't')",
    "insert into ref.equivalence_class values ('test_analytics_role', 'x', 'x')",
    "insert into core.series (series_key, entry_id, unit, resolution, kind) "
    "values ('test.analytics_role', 'x', 'x', interval '1 hour', 'actual')",
    "insert into ops.ingest_run (run_id, source, dataset, status) values (gen_random_uuid(), 't', 't', 'ok')",
    "insert into ops.bead_outbox (kind, title, body) values ('human', 't', 't')",
    "update ops.ingest_run set error = error",
    "delete from feat.feature_value where false",
]


ENTITY = "dk.zone.test_analytics_role"


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection]:  # type: ignore[type-arg]
    """One transaction, rolled back: a test entity written as the owner, then the role scanner_analytics."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    with psycopg.connect(url) as c:
        c.execute(
            "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
            "values ('dk','Denmark','active','Europe/Copenhagen','DKK','{}','t','t') on conflict do nothing"
        )
        c.execute(
            "insert into ref.entity (entity_id, kind, country_code, name) values (%s, 'zone', 'dk', 't')", (ENTITY,)
        )
        c.execute("set local role scanner_analytics")
        yield c
        c.rollback()


def test_analytics_role_reads_every_canonical_schema(conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
    for table in READ:
        conn.execute(f"select count(*) from {table}")


def test_analytics_role_writes_runs_and_derived_rows_only(conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
    run_id = uuid.uuid4()
    conn.execute(
        "insert into ops.compute_run (run_id, kind, name, status, code_version) values (%s, 'feature', 't', "
        "'running', 't')",
        (run_id,),
    )
    conn.execute("update ops.compute_run set status = 'ok' where run_id = %s", (run_id,))
    conn.execute(
        "insert into feat.feature_value (feature_key, entity_id, period_start, period_end, value, run_id, "
        "code_version) values ('test', %s, now(), now(), 1, %s, 't')",
        (ENTITY, run_id),
    )
    for statement in DENIED_WRITES:
        with pytest.raises(errors.InsufficientPrivilege), conn.transaction():
            conn.execute(statement)


def test_analytics_role_reads_serving_as_anon(conn: psycopg.Connection) -> None:  # type: ignore[type-arg]
    conn.execute("set local role anon")
    conn.execute("select count(*) from serving.zone_card")
    with pytest.raises(errors.InsufficientPrivilege):
        conn.execute("select count(*) from ref.scenario")
