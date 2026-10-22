"""Feature-run pruning and catch-up planning against a migrated database. Run with: TEST_DATABASE_URL=... pytest -m db

Inserts test-only feature runs (names starting with test_prune_), scenarios (test_prune_*) and ingest runs of a
test source, and removes them afterwards. Pruning is planned over the whole database (read only) and only the
test runs are applied, so other data is never touched.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
import pytest
from conftest import insert_scenario
from psycopg.types.json import Jsonb

from scanner.core.manifest import Manifest
from scanner.core.time import local_day_bounds
from scanner.runtime import catchup, prune
from scanner.runtime.context import RepoState

pytestmark = pytest.mark.db

NAME = "test_prune_feature"
ENTITY = "dk.zone.test_prune"
SETS = {"weights": "test_prune_set"}
T0 = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    with psycopg.connect(url) as c:
        _cleanup(c)
        c.execute(
            "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
            "values ('dk','Denmark','active','Europe/Copenhagen','DKK','{}','t','t') on conflict do nothing"
        )
        c.execute(
            "insert into ref.entity (entity_id, kind, country_code, name) values (%s, 'zone', 'dk', 'T')", (ENTITY,)
        )
        c.execute(
            "insert into ref.assumption_set (set_id, kind, status, owners, parameters, source_path, content_hash) "
            "values ('test_prune_set', 'weights', 'proposed', '{o}', '{}', 't', 't')"
        )
        c.commit()
        yield c
        _cleanup(c)


def _cleanup(c: psycopg.Connection[Any]) -> None:
    c.rollback()
    c.execute("delete from feat.score where entity_id = %s", (ENTITY,))
    c.execute("delete from feat.feature_value where entity_id = %s", (ENTITY,))
    c.execute("delete from ops.compute_run where name like 'test_prune_%' or scenario_id like 'test_prune_%'")
    c.execute("delete from ref.scenario where scenario_id like 'test_prune_%'")
    c.execute("delete from ref.assumption_set where set_id = 'test_prune_set'")
    c.execute("delete from ref.entity where entity_id = %s", (ENTITY,))
    c.execute("delete from ops.ingest_run where source = 'test_catchup'")
    c.commit()


def _run(
    c: psycopg.Connection[Any], kind: str, name: str, hours: int, status: str = "ok", scenario: str | None = None
) -> uuid.UUID:
    run_id = uuid.uuid4()
    c.execute(
        "insert into ops.compute_run (run_id, kind, name, status, code_version, params, scenario_id, started_at) "
        "values (%s, %s, %s, %s, 't', '{}', %s, %s)",
        (run_id, kind, name, status, scenario, T0 + timedelta(hours=hours)),
    )
    if kind == "feature":
        c.execute(
            "insert into feat.feature_value (feature_key, entity_id, period_start, period_end, value, run_id, "
            "code_version) values ('k', %s, %s, %s, 1, %s, 't')",
            (ENTITY, T0, T0 + timedelta(days=1), run_id),
        )
    return run_id


def _score(c: psycopg.Connection[Any], scenario: str, hours: int, feature_run: uuid.UUID) -> uuid.UUID:
    run_id = _run(c, "score", "congestion", hours, scenario=scenario)
    breakdown = {"features": {"k": {"run_id": str(feature_run)}}, "unverified_inputs": {str(feature_run): 0}}
    c.execute(
        "insert into feat.score (score_kind, entity_id, period_start, period_end, value, breakdown, weights_set_id, "
        "run_id, code_version) values ('congestion', %s, %s, %s, 50, %s, 'test_prune_set', %s, 't')",
        (ENTITY, T0, T0 + timedelta(days=1), Jsonb(breakdown), run_id),
    )
    return run_id


def _mine(plan: prune.PrunePlan) -> set[uuid.UUID]:
    return {r.run_id for r in plan.delete if r.name == NAME}


def test_prune_keeps_newest_and_runs_that_served_or_approved_scores_reference(conn: psycopg.Connection[Any]) -> None:
    insert_scenario(conn, "test_prune_exploratory", SETS)
    insert_scenario(conn, "test_prune_approved", SETS, approved=True)
    r = [_run(conn, "feature", NAME, h) for h in range(6)]  # r[5] is the newest
    _run(conn, "feature", NAME, 10, status="failed")  # newer than the kept good runs: kept
    _score(conn, "test_prune_exploratory", 20, r[0])  # superseded by the next score run of the scenario
    _score(conn, "test_prune_exploratory", 21, r[1])  # served: protects r[1]
    _score(conn, "test_prune_approved", 22, r[2])  # approved scenario, old output: protects r[2]
    _score(conn, "test_prune_approved", 23, r[5])
    conn.commit()

    plan = prune.plan_prune(conn, keep=2)
    assert _mine(plan) == {r[0], r[3]}
    assert plan.rows >= 2 and plan.protected >= 2

    everything = prune.plan_prune(conn, keep=2, protect="all")
    assert _mine(everything) == {r[3]}  # any score row protects, including the superseded one

    mine = prune.PrunePlan([x for x in plan.delete if x.name == NAME], 0, 0)
    assert prune.apply_prune(conn, mine) == 2
    left = {x[0] for x in conn.execute("select run_id from ops.compute_run where name = %s", (NAME,)).fetchall()}
    assert r[0] not in left and r[3] not in left and {r[1], r[2], r[4], r[5]} <= left
    assert conn.execute("select count(*) from feat.feature_value where run_id = %s", (r[0],)).fetchone() == (0,)


def test_prune_needs_keep_at_least_one(conn: psycopg.Connection[Any]) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        prune.plan_prune(conn, keep=0)


def _ingest_run(
    c: psycopg.Connection[Any],
    day: date,
    status: str,
    started: datetime,
    received: int | None = None,
    expected: int | None = None,
) -> None:
    c.execute(
        "insert into ops.ingest_run (run_id, source, dataset, partition_key, started_at, status, rows_received, "
        "rows_expected) values (%s, 'test_catchup', 'D', %s, %s, %s, %s, %s)",
        (uuid.uuid4(), day.isoformat(), started, status, received, expected),
    )


def test_catch_up_plans_days_without_a_full_day_run(conn: psycopg.Connection[Any], state: RepoState) -> None:
    base = next(m for m in state.manifests if m.dataset == "DayAheadPrices")
    m: Manifest = base.model_copy(
        update={"source": "test_catchup", "dataset": "D", "history_start_local": date(2026, 10, 1)}
    )
    tz = "Europe/Copenhagen"
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)  # yesterday is 2026-10-09
    end = lambda d: local_day_bounds(d, tz)[1]  # noqa: E731
    d = date(2026, 10, 1)
    _ingest_run(conn, d, "ok", end(d) + timedelta(hours=2), 96, 96)  # done
    d = date(2026, 10, 2)
    _ingest_run(conn, d, "ok", end(d) - timedelta(hours=1), None, None)  # only a run on the day itself: todo
    d = date(2026, 10, 3)
    _ingest_run(conn, d, "failed", end(d) + timedelta(hours=2))  # failed: todo
    d = date(2026, 10, 4)
    _ingest_run(conn, d, "blocked", end(d) + timedelta(hours=2))  # hard deviation: a person decides
    d = date(2026, 10, 5)
    _ingest_run(conn, d, "ok", end(d) + timedelta(hours=2), 90, 96)  # full-day run, fewer rows: a gap
    conn.commit()

    plan = catchup.plan_dataset(conn, m, tz, None, now)
    assert plan.todo == [date(2026, 10, 2), date(2026, 10, 3), *(date(2026, 10, i) for i in range(6, 10))]
    assert plan.blocked == [date(2026, 10, 4)]
    assert plan.incomplete == [date(2026, 10, 5)]
    assert catchup.plan_dataset(conn, m, tz, date(2026, 10, 8), now).todo == [date(2026, 10, 8), date(2026, 10, 9)]
    assert catchup.last_day(m.model_copy(update={"history_end_local": date(2026, 10, 7)}), tz, now) == date(2026, 10, 7)
