"""Serving views and the static export (WP1.10, WP1.11, docs/ui-contract.md).

Offline: the contract document parses, GeoJSON building. Database tests (marked db; TEST_DATABASE_URL):
the contract tests compare every serving view with docs/ui-contract.md and check what role anon can read;
test_views_* insert test rows (IDs containing test_serving) in a transaction that is rolled back;
test_export_* commits a test zone and station, exports, and removes them.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from conftest import insert_scenario
from psycopg.types.json import Jsonb

from scanner import REPO_ROOT
from scanner.core.registry import PROPOSED_LABEL
from scanner.runtime.export import GEO_COLUMNS, export_static, feature_collection, labels_in
from scanner.runtime.report import write_report

CONTRACT = REPO_ROOT / "docs" / "ui-contract.md"
# Types as written in the contract -> information_schema.columns.udt_name.
UDT = {
    "text": "text",
    "float": "float8",
    "integer": "int4",
    "bigint": "int8",
    "boolean": "bool",
    "uuid": "uuid",
    "timestamptz": "timestamptz",
    "date": "date",
    "json": "jsonb",
    "text[]": "_text",
    "uuid[]": "_uuid",
}
# The raw-data views (docs/ui-contract.md, "Raw data"): role authenticated only, never anon.
RAW_VIEWS = {
    "series": [
        "series_key",
        "entity_id",
        "entity_name",
        "entity_kind",
        "country_code",
        "entry_id",
        "source",
        "dataset",
        "field",
        "unit",
        "resolution",
        "kind",
        "licence",
    ],
    "observations": [
        "series_key",
        "interval_start",
        "interval_end",
        "value",
        "issued_at",
        "ingested_at",
        "review_status",
        "run_id",
    ],
    "entities": ["entity_id", "kind", "country_code", "parent_id", "name", "eic"],
    "facts": [
        "fact_id",
        "entity_id",
        "attribute",
        "value_num",
        "value_text",
        "value_json",
        "unit",
        "valid_from",
        "valid_to",
        "known_at",
        "source",
        "source_url",
        "run_id",
    ],
    "feature_values": [
        "feature_key",
        "entity_id",
        "period_start",
        "period_end",
        "statistic",
        "value",
        "value_text",
        "run_id",
        "method_id",
        "method_version",
        "computed_at",
    ],
    "scores_all": [
        "scenario_id",
        "entity_id",
        "score_kind",
        "score",
        "breakdown",
        "period_start",
        "period_end",
        "weights_set_id",
        "run_id",
        "computed_at",
    ],
}
# Serving views that do not filter on sensitivity in their own definition, and why that is safe.
NO_SENSITIVITY_FILTER = {
    "feature_catalog": "static titles, units and caveats of the score components; no data rows",
    "latest_runs": "run metadata only (kind, name, status, times, code version) from ops.compute_run; no values",
    "opportunities": "reads feat.scenario_opportunity, which keeps public rows of public entities",
    "return_ranges": "reads feat.scenario_return_range, which keeps public runs, results and entities",
}
# Views documented in prose ("Supporting views"), with their columns in order.
SUPPORTING_VIEWS = {
    "feature_catalog": [
        "weight_key",
        "feature_key",
        "statistic",
        "group_title",
        "title",
        "unit",
        "caveat",
        "description",
        "display_order",
    ],
    "latest_runs": [
        "kind",
        "name",
        "run_id",
        "status",
        "started_at",
        "finished_at",
        "code_version",
        "last_attempt_status",
        "last_attempt_at",
        "scenario_id",
    ],
    "opportunities": [
        "entity_id",
        "asset_type",
        "matched",
        "status",
        "reasons",
        "run_id",
        "computed_at",
        "scenario_id",
    ],
    "return_ranges": [
        "entity_id",
        "asset_type",
        "metric",
        "low",
        "mid",
        "high",
        "unit",
        "labels",
        "method",
        "method_version",
        "template_set_id",
        "run_id",
        "computed_at",
        "valuation_run_id",
        "price_window_start",
        "price_window_end",
        "proposed_assumptions",
        "days_valued",
        "skipped_days",
        "complete_365_day_windows",
        "unverified_values",
        "scenario_id",
    ],
}
# GeoJSON nesting depth of `coordinates` per geometry type.
DEPTH = {"Point": 0, "MultiPoint": 1, "LineString": 1, "Polygon": 2, "MultiLineString": 2, "MultiPolygon": 3}


def documented_columns() -> dict[str, list[tuple[str, str]]]:
    """view -> [(column, udt_name)] from the column tables under the '### `serving.<view>`' headings."""
    out: dict[str, list[tuple[str, str]]] = {}
    view: str | None = None
    for line in CONTRACT.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^### `serving\.(\w+)`", line):
            view = m[1]
            out[view] = []
        elif line.startswith("#"):
            view = None
        elif view and (m := re.match(r"^\| `(\w+)` \| ([\w\[\]]+) \|", line)):
            out[view].append((m[1], UDT[m[2]]))
    return out


def _positions(coords: Any, depth: int) -> list[Any]:
    return [coords] if depth == 0 else [p for c in coords for p in _positions(c, depth - 1)]


def assert_geojson(fc: Any) -> None:
    """A valid RFC 7946 FeatureCollection in EPSG:4326 (null geometries allowed)."""
    assert fc["type"] == "FeatureCollection" and isinstance(fc["features"], list)
    for f in fc["features"]:
        assert f["type"] == "Feature" and isinstance(f["properties"], dict)
        g = f["geometry"]
        if g is None:
            continue
        positions = _positions(g["coordinates"], DEPTH[g["type"]])
        assert positions
        for lon, lat, *_ in positions:
            assert -180 <= lon <= 180 and -90 <= lat <= 90


def test_contract_documents_the_ui_views() -> None:
    documented = documented_columns()
    assert set(documented) == {
        "zone_map",
        "zone_card",
        "zone_feature_breakdown",
        "substation_map",
        "ranked_sites",
        "source_freshness",
        "source_completeness",
        "scenario_catalog",
        "assumption_register",
        "method_catalog",
        "scenario_comparison",
    }
    assert all(cols for cols in documented.values())
    for view in ("zone_map", "zone_card", "zone_feature_breakdown", "ranked_sites"):
        assert ("scenario_id", "text") in documented[view], view
    assert set(GEO_COLUMNS) <= set(documented)
    for view, column in GEO_COLUMNS.items():
        assert (column, "jsonb") in documented[view]
    text = CONTRACT.read_text(encoding="utf-8")
    for label in (PROPOSED_LABEL, "zone-level", "unverified data", "proxy", "current snapshot", "low_station_count"):
        assert f"`{label}`" in text
    for view in ("ranked_sites", "substation_map"):
        assert {("capacity_map_caveat", "text"), ("capacity_map_caveat_en", "text")} <= set(documented[view]), view
    assert {("no_station_data", "bool"), ("low_station_count", "bool")} <= set(documented["ranked_sites"])
    assert ("consumption_not_assessed", "bool") in documented["substation_map"]


def test_feature_collection_keeps_rows_without_geometry() -> None:
    rows = [
        {"entity_id": "a", "geometry": {"type": "Point", "coordinates": [10.0, 55.0]}, "v": 1},
        {"entity_id": "b", "geometry": None, "v": None},
    ]
    fc = feature_collection(rows, "geometry")
    assert_geojson(fc)
    assert [f["id"] for f in fc["features"]] == ["a", "b"]
    assert fc["features"][1] == {
        "type": "Feature",
        "id": "b",
        "geometry": None,
        "properties": {"entity_id": "b", "v": None},
    }
    with pytest.raises(AssertionError):
        assert_geojson(
            {
                "type": "FeatureCollection",
                "features": [{**fc["features"][0], "geometry": {"type": "Point", "coordinates": [200.0, 55.0]}}],
            }
        )


def test_labels_in_reads_label_columns() -> None:
    rows = [{"labels": ["a"], "revenue_labels": ["b", "a"], "caveat": "proxy", "other": ["x"]}, {"caveat": None}]
    assert labels_in(rows) == {"a", "b", "proxy"}


ZONE, HIDDEN_ZONE, LICENSED_ZONE = "dk.zone.test_serving", "dk.zone.test_serving_hidden", "dk.zone.test_serving_lic"
MUN, LICENSED_MUN = "dk.municipality.test_serving", "dk.municipality.test_serving_lic"
S1, S2, S3 = (f"dk.substation.test_serving.s{i}" for i in (1, 2, 3))
SRC, DOC, SET = "test_serving_src", "test_serving:doc", "test_serving_set"
OFFICIAL, OTHER = "test_serving_official", "test_serving_other"  # the official scenario only in the test
LB, UB = "dk.test_serving.Cap.lim_lb", "dk.test_serving.Cap.lim_ub"
WINDOW = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))
SQUARE = "POLYGON((10 55, 10.1 55, 10.1 55.1, 10 55.1, 10 55))"


def _url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    return url


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection[Any]]:
    c = psycopg.connect(_url())
    yield c
    c.rollback()
    c.close()


def _one(conn: psycopg.Connection[Any], sql: str, *args: Any) -> Any:
    row = conn.execute(sql, args).fetchone()
    assert row is not None, sql
    return row[0] if len(row) == 1 else row


def caveat_text(conn: psycopg.Connection[Any]) -> str:
    return str(_one(conn, "select caveat from feat.capacity_map_caveat"))


def _serving_views(conn: psycopg.Connection[Any]) -> list[str]:
    return [v for (v,) in conn.execute("select viewname from pg_views where schemaname = 'serving' order by 1")]


@pytest.mark.db
def test_every_serving_view_matches_the_contract(conn: psycopg.Connection[Any]) -> None:
    documented = documented_columns()
    assert set(_serving_views(conn)) == set(documented) | set(SUPPORTING_VIEWS) | set(RAW_VIEWS)
    columns = (
        "select column_name, udt_name from information_schema.columns "
        "where table_schema = 'serving' and table_name = %s order by ordinal_position"
    )
    for view, cols in documented.items():
        assert conn.execute(columns, (view,)).fetchall() == cols, view
    for view, names in (SUPPORTING_VIEWS | RAW_VIEWS).items():
        assert [c for c, _ in conn.execute(columns, (view,))] == names, view


@pytest.mark.db
def test_anon_reads_every_serving_view_and_nothing_else(conn: psycopg.Connection[Any]) -> None:
    views = [v for v in _serving_views(conn) if v not in RAW_VIEWS]
    conn.execute("set local role anon")
    for view in views:
        with conn.transaction():
            conn.execute(f'select * from serving."{view}" limit 1')
    for table in (
        "ref.entity",
        "ref.dataset",
        "ref.scenario",
        "ref.method_card",
        "core.observation",
        "core.fact",
        "core.preferred_series",
        "feat.feature_value",
        "feat.score",
        "feat.latest_score",
        "feat.scenario_score",
        "val.run",
        "val.result",
        "ops.compute_run",
        "ops.ingest_run",
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(f"select 1 from {table} limit 1")


@pytest.mark.db
def test_raw_views_are_for_authenticated_only(conn: psycopg.Connection[Any]) -> None:
    conn.execute("set local role anon")
    for view in RAW_VIEWS:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(f'select * from serving."{view}" limit 1')
    conn.execute("reset role")
    conn.execute("set local role authenticated")
    for view in RAW_VIEWS:
        with conn.transaction():
            conn.execute(f'select * from serving."{view}" limit 1')


@pytest.mark.db
def test_raw_observations_match_the_effective_view_and_hide_rejected_runs(conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
        "values ('dk','Denmark','active','Europe/Copenhagen','DKK','{}','t','t') on conflict do nothing"
    )
    conn.execute(
        "insert into ref.entity (entity_id, kind, country_code, name) values (%s, 'zone', 'dk', 'raw test')", (ZONE,)
    )
    conn.execute("insert into ref.equivalence_class values ('test_serving_raw', 'x', 'x') on conflict do nothing")
    conn.execute(
        "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, equivalence_class, kind, "
        "unit, resample, definition, status, source_path, content_hash) values ('dk.test_serving.Raw.P', "
        "'test_serving', 'Raw', 'dk', 'P', 'test_serving_raw', 'actual', 'EUR/MWh', 'mean', '{}', 'draft', 't', 't')"
    )
    sid = _one(
        conn,
        "insert into core.series (series_key, entry_id, entity_id, unit, resolution, kind) "
        "values ('test_serving.raw', 'dk.test_serving.Raw.P', %s, 'EUR/MWh', interval '1 hour', 'actual') "
        "returning series_id",
        ZONE,
    )
    runs = {name: uuid.uuid4() for name in ("ok", "accepted", "rejected", "pending")}
    for name, run in runs.items():
        conn.execute(
            "insert into ops.ingest_run (run_id, source, dataset, status) values (%s, 'test_serving', %s, 'ok')",
            (run, name),
        )
    rows = [  # hour, run, quality, ingested_at hour
        (0, "ok", "ok", 0),
        (1, "accepted", "quarantined", 0),
        (2, "rejected", "quarantined", 0),
        (3, "pending", "quarantined", 0),
        (4, "ok", "ok", 0),
        (4, "ok", "ok", 1),  # a later ingest of hour 4 replaces the earlier one
    ]
    for hour, run, quality, ingested in rows:
        start = datetime(2026, 1, 1, hour, tzinfo=UTC)
        conn.execute(
            "insert into core.observation (series_id, interval_start, interval_end, value, run_id, quality, "
            "ingested_at) values (%s, %s, %s + interval '1 hour', %s, %s, %s, %s + interval '1 minute' * %s)",
            (sid, start, start, hour + ingested / 10, runs[run], quality, start, ingested),
        )
    for run, decision in ((runs["accepted"], "accept"), (runs["rejected"], "reject")):
        conn.execute(
            "insert into ops.quarantine_decision (run_id, decided_by, decision, reason) values (%s, 't', %s, 't')",
            (run, decision),
        )
    got = conn.execute(
        "select interval_start, value, review_status from serving.observations where series_key = 'test_serving.raw' "
        "order by interval_start"
    ).fetchall()
    expected = conn.execute(
        "select interval_start, value, review_status from core.observation_effective where series_id = %s "
        "and usable order by interval_start",
        (sid,),
    ).fetchall()
    assert got == expected
    assert [(r[0].hour, r[2]) for r in got] == [(0, "ok"), (1, "verified"), (3, "unverified"), (4, "ok")]
    assert got[-1][1] == 4.1


@pytest.mark.db
def test_every_serving_view_filters_on_sensitivity_or_says_why_not(conn: psycopg.Connection[Any]) -> None:
    unfiltered = {
        view
        for view in _serving_views(conn)
        if "sensitivity" not in _one(conn, "select pg_get_viewdef(%s::regclass)", f'serving."{view}"')
    }
    assert unfiltered == set(NO_SENSITIVITY_FILTER)


def _run(conn: psycopg.Connection[Any], kind: str, name: str, scenario: str | None = OFFICIAL) -> uuid.UUID:
    run_id = uuid.uuid4()
    conn.execute(
        "insert into ops.compute_run (run_id, kind, name, status, code_version, scenario_id) "
        "values (%s, %s, %s, 'ok', 'test', %s)",
        (run_id, kind, name, None if kind == "feature" else scenario),
    )
    return run_id


def _entity(
    conn: psycopg.Connection[Any],
    entity_id: str,
    kind: str,
    parent: str | None = None,
    geom: str | None = None,
    sensitivity: str = "public",
    geom_sensitivity: str = "public",
    attributes: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "insert into ref.entity (entity_id, kind, country_code, parent_id, name, attributes, sensitivity) "
        "values (%s, %s, 'dk', %s, %s, %s, %s)",
        (entity_id, kind, parent, entity_id.rsplit(".", 1)[-1], Jsonb(attributes or {}), sensitivity),
    )
    if geom:
        conn.execute(
            "insert into ref.entity_geometry (entity_id, geom, source, sensitivity) "
            "values (%s, st_geomfromtext(%s, 4326), 'test', %s)",
            (entity_id, geom, geom_sensitivity),
        )


def _score(
    conn: psycopg.Connection[Any],
    run: uuid.UUID,
    entity: str,
    value: float,
    sensitivity: str = "public",
    scenario: str = OFFICIAL,
    flags: tuple[str, ...] = (),
) -> None:
    feature_run = str(uuid.uuid4())
    component = {
        "raw": 100.0,
        "percentile": value,
        "weight": 100.0,
        "contribution": value,
        "weight_key": "f1_price_spread",
        "statistic": "mean",
        "set_weight": 15.0,
        "run_id": feature_run,
        "period": [WINDOW[0].isoformat(), WINDOW[1].isoformat()],
        "population": {"method": "pooled_daily_values", "n": 10},
        **({"inherited_from": ZONE} if entity != ZONE else {}),
    }
    breakdown = {
        "features": {"f1_intraday_spread_hourly": component},
        "labels": [PROPOSED_LABEL],
        "proposed_assumptions": True,
        "missing_features": ["f3_interconnector_saturation"],
        "unverified_inputs": {feature_run: 2},
        "unverified_values": 2,
        "flags": list(flags),
        **({"parent_zone": ZONE} if entity != ZONE else {}),
    }
    conn.execute(
        "insert into feat.score (score_kind, entity_id, period_start, period_end, value, breakdown, weights_set_id, "
        "run_id, code_version, sensitivity, scenario_id) "
        "values ('congestion', %s, %s, %s, %s, %s, %s, %s, 'test', %s, %s)",
        (entity, *WINDOW, value, Jsonb(breakdown), SET, run, sensitivity, scenario),
    )


def _valuation(
    conn: psycopg.Connection[Any],
    run: uuid.UUID,
    low: float,
    high: float,
    sensitivity: str,
    later: bool = False,
    scenario: str = OFFICIAL,
    entity: str = ZONE,
) -> None:
    vid = uuid.uuid4()
    conn.execute(
        "insert into val.run (valuation_run_id, entity_id, asset_type, template_set_id, method, method_version, "
        "price_window_start, price_window_end, inputs, labels, run_id, sensitivity, computed_at, scenario_id) "
        "values (%s, %s, 'bess', %s, 'bess_v0_daily_lp_day_ahead', '1', %s, %s, %s, %s, %s, %s, "
        "now() + %s * interval '1 minute', %s)",
        (
            vid,
            entity,
            SET,
            *WINDOW,
            Jsonb({"proposed_assumptions": True, "days_valued": 31, "skipped_days": 0, "unverified_values": 3}),
            ["zone-level", "perfect-foresight upper bound", "day-ahead arbitrage only", PROPOSED_LABEL],
            run,
            sensitivity,
            1 if later else 0,
            scenario,
        ),
    )
    conn.execute(
        "insert into val.result (valuation_run_id, metric, low, high, unit, sensitivity, scenario_id) "
        "values (%s, 'revenue_eur_per_mw_year', %s, %s, 'EUR/MW/year', %s, %s)",
        (vid, low, high, sensitivity, scenario),
    )


def _fixture_rows(conn: psycopg.Connection[Any]) -> dict[str, uuid.UUID]:
    """Reference rows, an official and an exploratory test scenario (the database's own official scenario is
    set aside in the test's transaction), public and licensed scores, opportunities and ranges, capacity facts,
    F7 values, and a stale dataset feeding the test zone. Returns the official scenario's run IDs."""
    conn.execute(
        "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
        "values ('dk', 'Denmark', 'active', 'Europe/Copenhagen', 'DKK', '{}', 't', 't') on conflict do nothing"
    )
    conn.execute(
        "insert into ref.assumption_set (set_id, kind, status, owners, parameters, source_path, content_hash) "
        "values (%s, 'test', 'proposed', '{owner}', %s, 'test', 'h')",
        (SET, Jsonb({"weights": {"value": None, "proposed": {"f1_price_spread": 15}, "note": "test"}})),
    )
    conn.execute("update ref.scenario set status = 'exploratory' where status = 'official'")
    insert_scenario(conn, OFFICIAL, {"score_weights": SET}, status="official")
    insert_scenario(conn, OTHER, {"score_weights": SET}, method_switches={"score_weights.weights": {"x": 1}})
    _entity(conn, ZONE, "zone", geom=SQUARE, attributes={"short": "TS"})
    _entity(conn, HIDDEN_ZONE, "zone", geom=SQUARE, sensitivity="licensed")
    _entity(conn, LICENSED_ZONE, "zone", geom=SQUARE)
    _entity(conn, MUN, "municipality", parent=ZONE, geom=SQUARE)
    _entity(conn, LICENSED_MUN, "municipality", parent=ZONE, geom=SQUARE)
    station = {"dso": None, "municipality": MUN, "listed": True}
    _entity(conn, S1, "substation", parent=ZONE, geom="POINT(10.05 55.05)", attributes=station)
    _entity(conn, S2, "substation", parent=ZONE, geom="POINT(10.06 55.05)", geom_sensitivity="licensed")
    _entity(conn, S3, "substation", parent=ZONE, geom="POINT(10.07 55.05)", sensitivity="licensed")
    conn.execute(
        "insert into ref.benchmark (benchmark_id, entity_id, zone_id, description, expected, owner, source_path) "
        "values ('test_serving_benchmark', %s, %s, 'test', '{}', 'test', 'test')",
        (MUN, ZONE),
    )

    score_run = _run(conn, "score", "congestion")
    _score(conn, score_run, ZONE, 80.0)
    _score(conn, score_run, MUN, 70.0)
    _score(conn, score_run, LICENSED_ZONE, 90.0, sensitivity="licensed")
    _score(conn, score_run, LICENSED_MUN, 95.0, sensitivity="licensed")
    opp_run = _run(conn, "score", "opportunity_types")
    for asset, status, sensitivity in (("bess", "match", "public"), ("dsf", "no_match", "licensed")):
        _opportunity(conn, opp_run, ZONE, asset, status, sensitivity)
    val_run = _run(conn, "valuation", "bess_return_range_v0")
    _valuation(conn, val_run, 10.0, 20.0, "public")
    _valuation(conn, val_run, 99.0, 999.0, "licensed", later=True)
    # An older official run whose rows must not be served (one consistent run per scenario).
    old = _run(conn, "score", "congestion")
    conn.execute("update ops.compute_run set started_at = now() - interval '1 day' where run_id = %s", (old,))
    _score(conn, old, LICENSED_ZONE, 10.0)
    # The exploratory scenario: the zone scores 60 there, the site 75 with a flag; ranges 12 to 22.
    other = _run(conn, "score", "congestion", scenario=OTHER)
    _score(conn, other, ZONE, 60.0, scenario=OTHER)
    _score(conn, other, MUN, 75.0, scenario=OTHER, flags=("no_station_data",))
    _valuation(
        conn, _run(conn, "valuation", "bess_return_range_v0", scenario=OTHER), 12.0, 22.0, "public", False, OTHER
    )

    for cls in ("capacity.free_mw_limited.consumption", "test_serving_class"):
        conn.execute("insert into ref.equivalence_class values (%s, 'x', 'x') on conflict do nothing", (cls,))
    entries = [
        (LB, "Cap", "capacity.free_mw_limited.consumption", {"access": "limited", "bound": "lower"}, "capacity"),
        (UB, "Cap", "capacity.free_mw_limited.consumption", {"access": "limited", "bound": "upper"}, "capacity"),
        ("dk.test_serving.TestServing.P", "TestServing", "test_serving_class", {}, "actual"),
    ]
    for entry_id, dataset, cls, dims, kind in entries:
        conn.execute(
            "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, dimensions, "
            "equivalence_class, kind, unit, resample, definition, status, source_path, content_hash) "
            "values (%s, %s, %s, 'dk', 'x', %s, %s, %s, 'MW', 'none', '{}', 'draft', 't', 't')",
            (entry_id, SRC, dataset, Jsonb(dims), cls, kind),
        )
    conn.execute(
        "insert into core.source_document (document_id, source, fetched_at, sha256, raw_uri, content_type) "
        "values (%s, %s, now(), 'x', 'file://x', 'application/json')",
        (DOC, SRC),
    )
    cap_run = uuid.uuid4()
    for entry_id, value, sensitivity in ((LB, 5.0, "public"), (UB, 25.0, "licensed")):
        conn.execute(
            "insert into core.fact (entity_id, attribute, value_num, unit, valid_from, known_at, source_document_id, "
            "run_id, sensitivity) values (%s, %s, %s, 'MW', '2026-09-01T00:00Z', now(), %s, %s, %s)",
            (S1, entry_id, value, DOC, cap_run, sensitivity),
        )
    f7_run = _run(conn, "feature", "f7_substation_saturation")
    for entity, stat, value, sensitivity in (
        (S1, "free_mw_production", 0.0, "public"),
        (S1, "free_mw_consumption", 30.0, "licensed"),
        (MUN, "stations", 3.0, "public"),
    ):
        conn.execute(
            "insert into feat.feature_value (feature_key, entity_id, period_start, period_end, statistic, value, "
            "run_id, code_version, sensitivity) values ('f7_substation_saturation', %s, %s, %s, %s, %s, %s, 't', %s)",
            (entity, *WINDOW, stat, value, f7_run, sensitivity),
        )

    # A series of the zone from a dataset whose last successful run (three hours ago) is older than its SLA.
    conn.execute(
        "insert into ops.ingest_run (run_id, source, dataset, partition_key, started_at, finished_at, status, "
        "rows_expected, rows_received) values (%s, %s, 'TestServing', '2026-01-10', now() - interval '3 hours', "
        "now() - interval '3 hours', 'ok', 96, 95)",
        (uuid.uuid4(), SRC),
    )
    for dataset, sensitivity in (("TestServing", "public"), ("TestServingHidden", "licensed")):
        conn.execute(
            "insert into ref.dataset (source, dataset, kind, status, schedule, freshness_sla, sensitivity, "
            "source_path) values (%s, %s, 'manifest', 'active', '{}', 'PT1H', %s, 'test')",
            (SRC, dataset, sensitivity),
        )
    for dataset in ("TestServingHidden", "TestServingLoader"):
        conn.execute(
            "insert into ops.ingest_run (run_id, source, dataset, status) values (%s, %s, %s, 'ok')",
            (uuid.uuid4(), SRC, dataset),
        )
    conn.execute(
        "insert into core.series (series_key, entry_id, entity_id, unit, resolution, kind) "
        "values ('test_serving.p', 'dk.test_serving.TestServing.P', %s, 'MW', interval '1 hour', 'actual')",
        (ZONE,),
    )
    return {"score": score_run, "opportunity": opp_run, "valuation": val_run}


def _opportunity(
    conn: psycopg.Connection[Any], run: uuid.UUID, entity: str, asset: str, status: str, sensitivity: str = "public"
) -> None:
    conn.execute(
        "insert into feat.opportunity (entity_id, asset_type, matched, status, reasons, rules_set_id, run_id, "
        "sensitivity, scenario_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            entity,
            asset,
            status == "match",
            status,
            Jsonb({"conditions": [], "proposed_assumptions": True}),
            SET,
            run,
            sensitivity,
            OFFICIAL,
        ),
    )


@pytest.mark.db
def test_views_serve_public_rows_with_labels_and_flags(conn: psycopg.Connection[Any]) -> None:
    _fixture_rows(conn)

    card = conn.execute(
        "select congestion_score, short_name, best_fit_asset_types, asset_types, revenue_low_eur_per_mw_year, "
        "revenue_high_eur_per_mw_year, revenue_labels, missing_features, score_unverified_values, "
        "revenue_unverified_values, unverified_data, stale_datasets, stale_source, proposed_assumptions, labels "
        "from serving.zone_card where entity_id = %s",
        (ZONE,),
    ).fetchone()
    assert card is not None
    score, short, best, assets, low, high, rev_labels, missing, *flags, stale, stale_flag, proposed, labels = card
    assert (score, short, best, low, high) == (80.0, "TS", ["bess"], 10.0, 20.0)  # licensed range and dsf hidden
    assert [a["asset_type"] for a in assets] == ["bess"] and assets[0]["status_label"] == "match"
    assert rev_labels[0] == "zone-level" and missing == ["f3_interconnector_saturation"]
    assert flags == [2, 3, True]  # unverified values recorded by the score and by the valuation
    assert (stale, stale_flag, proposed) == ([f"{SRC}.TestServing"], True, True)
    assert labels == [PROPOSED_LABEL, "unverified data", "stale source"]
    # Its latest score is licensed; an older run's public row is not mixed in (one run per scenario).
    assert _one(conn, "select count(*) from serving.zone_card where entity_id = %s", LICENSED_ZONE) == 0
    for view in ("zone_card", "zone_map", "zone_feature_breakdown", "ranked_sites", "opportunities", "return_ranges"):
        served = f"select array_agg(distinct scenario_id) from serving.{view} where scenario_id is not null"
        assert _one(conn, served) == [OFFICIAL], view

    breakdown = conn.execute(
        "select feature_key, title, unit, caveat, contribution, unverified_values, run_id "
        "from serving.zone_feature_breakdown where entity_id = %s",
        (ZONE,),
    ).fetchall()
    assert [r[:6] for r in breakdown] == [
        ("f1_intraday_spread_hourly", "Intraday price spread (hourly prices)", "EUR/MWh", None, 80.0, 2)
    ]

    zones = dict(conn.execute("select entity_id, labels from serving.zone_map where entity_id like '%%test_serving%%'"))
    assert set(zones) == {ZONE, LICENSED_ZONE} and zones[LICENSED_ZONE] == []  # no public score: no card values

    site = conn.execute(
        "select zone_id, revenue_low_eur_per_mw_year, revenue_entity_id, is_benchmark, f7_stations, unverified_data, "
        "rank_in_zone, point ->> 'type' from serving.ranked_sites where entity_id = %s",
        (MUN,),
    ).fetchone()
    assert site == (ZONE, 10.0, ZONE, True, 3.0, True, 1, "Point")
    assert _one(conn, "select count(*) from serving.ranked_sites where entity_id = %s", LICENSED_MUN) == 0
    card = (
        "select capacity_map_caveat, capacity_map_caveat_en, no_station_data, low_station_count, "
        "f7_sample_stations from serving.ranked_sites where entity_id = %s"
    )
    caveat, caveat_en, no_data, low, sample = conn.execute(card, (MUN,)).fetchone() or ()
    assert caveat.startswith("Kapacitetskortet må på ingen måde tolkes som en forpligtigelse")
    assert caveat_en.endswith("(Our translation.)")
    assert (no_data, low, sample) == (False, False, None)
    # A site score flagged by the run: few stations behind its F7 statistic (scanner.scores.run).
    conn.execute(
        "update feat.score set breakdown = breakdown || %s where entity_id = %s and scenario_id = %s",
        (
            Jsonb(
                {
                    "flags": ["low_station_count"],
                    "station_sample": {
                        "f7_substation_saturation": {"statistic": "x", "stations": 2, "min_stations": 3}
                    },
                }
            ),
            MUN,
            OFFICIAL,
        ),
    )
    flagged = "select no_station_data, low_station_count, f7_sample_stations, score_flags from serving.ranked_sites "
    assert conn.execute(flagged + "where entity_id = %s", (MUN,)).fetchone() == (
        False,
        True,
        2.0,
        ["low_station_count"],
    )

    stations = conn.execute(
        "select entity_id, status_production, status_consumption, free_mw_consumption, limited_consumption_mw_low, "
        "limited_consumption_mw_high, capacity, publication_date::text, network_level, f7_caveat "
        "from serving.substation_map where entity_id like 'dk.substation.test_serving.%%'"
    ).fetchall()
    assert [s[0] for s in stations] == [S1]  # S2 has a licensed point, S3 is a licensed entity
    _, prod, cons, cons_mw, lb, ub, capacity, published, level, caveat = stations[0]
    assert (prod, cons, cons_mw, lb, ub) == ("no_free_capacity", "unknown", None, 5.0, None)
    assert set(capacity) == {LB} and (published, level, caveat) == ("2026-09-01", "distribution", "current snapshot")
    assessed = "select consumption_not_assessed, capacity_map_caveat from serving.substation_map where entity_id = %s"
    not_assessed, station_caveat = conn.execute(assessed, (S1,)).fetchone() or ()
    assert not_assessed is False and station_caveat == caveat_text(conn)
    conn.execute("update ref.entity set attributes = attributes - 'dso' where entity_id = %s", (S1,))
    assert conn.execute(assessed, (S1,)).fetchone()[0] is True  # type: ignore[index]  # a transmission station

    fresh = {
        d: (k, s, e, r)
        for d, k, s, e, r in conn.execute(
            "select dataset, kind, stale, rows_expected, rows_received from serving.source_freshness where source = %s",
            (SRC,),
        )
    }
    assert fresh == {"TestServing": ("manifest", True, 96, 95), "TestServingLoader": ("loader", None, None, None)}


@pytest.mark.db
def test_opportunities_and_ranges_hide_non_public_entities(conn: psycopg.Connection[Any]) -> None:
    """Public rows of a licensed entity are not served (the other views filter on the entity too)."""
    runs = _fixture_rows(conn)
    _opportunity(conn, runs["opportunity"], HIDDEN_ZONE, "bess", "match")
    _valuation(conn, runs["valuation"], 1.0, 2.0, "public", entity=HIDDEN_ZONE)
    for view in ("opportunities", "return_ranges"):
        assert _one(conn, f"select count(*) from serving.{view} where entity_id = %s", HIDDEN_ZONE) == 0
        assert _one(conn, f"select count(*) from serving.{view} where entity_id = %s", ZONE) > 0


@pytest.mark.db
def test_scenario_views_compare_catalog_and_register(conn: psycopg.Connection[Any]) -> None:
    runs = _fixture_rows(conn)
    compared = {
        (e, s): r
        for e, s, *r in conn.execute(
            "select entity_id, scenario_id, scenario_status, congestion_score, rank, revenue_low_eur_per_mw_year, "
            "score_delta, rank_delta, revenue_low_delta, score_flags, official_scenario_id, is_benchmark "
            "from serving.scenario_comparison where entity_id in (%s, %s, %s)",
            (ZONE, MUN, LICENSED_ZONE),
        )
    }
    assert set(compared) == {(ZONE, OFFICIAL), (ZONE, OTHER), (MUN, OFFICIAL), (MUN, OTHER)}
    assert compared[ZONE, OFFICIAL][:2] == ["official", 80.0]
    assert compared[ZONE, OFFICIAL][4:7] == [0.0, 0, 0.0]  # the official scenario against itself
    status, score, _, low, delta, _, low_delta, flags, official, benchmark = compared[ZONE, OTHER]
    assert (status, score, low, delta, low_delta, flags, official, benchmark) == (
        "exploratory",
        60.0,
        12.0,
        -20.0,
        2.0,
        [],
        OFFICIAL,
        False,
    )
    _, score, _, low, delta, _, _, flags, _, benchmark = compared[MUN, OTHER]
    assert (score, low, delta, flags, benchmark) == (75.0, 12.0, 5.0, ["no_station_data"], True)  # the zone's range

    catalog = {
        s: (status, runs_)
        for s, status, *runs_ in conn.execute(
            "select scenario_id, status, score_run_id, opportunity_run_id, valuation_run_id "
            "from serving.scenario_catalog where scenario_id like 'test\\_serving\\_%%'"
        )
    }
    assert catalog[OFFICIAL] == ("official", [runs["score"], runs["opportunity"], runs["valuation"]])
    assert catalog[OTHER][0] == "exploratory" and catalog[OTHER][1][1] is None
    hashes = _one(conn, "select assumption_hashes from serving.scenario_catalog where scenario_id = %s", OFFICIAL)
    assert hashes == {SET: "h"}

    register = conn.execute(
        "select parameter, value, proposed, status, note, scenarios, scenario_values, roles "
        "from serving.assumption_register where set_id = %s",
        (SET,),
    ).fetchall()
    assert register == [
        (
            "weights",
            None,
            {"f1_price_spread": 15},
            "proposed_only",
            "test",
            [OFFICIAL, OTHER],
            {OTHER: {"x": 1}},
            ["score_weights"],
        )
    ]
    cards = {m: (v, p) for m, v, p in conn.execute("select method_id, version, parameters from serving.method_catalog")}
    for method_id, (version, parameters) in cards.items():  # loaded from docs/methods when the DB has curated data
        assert version >= 1 and all("." in p for p in parameters), method_id


@pytest.mark.db
def test_unverified_data_is_false_not_null_without_unverified_values(conn: psycopg.Connection[Any]) -> None:
    _fixture_rows(conn)
    conn.execute("update feat.score set breakdown = breakdown - 'unverified_values' - 'unverified_inputs'")
    conn.execute("update val.run set inputs = inputs - 'unverified_values'")
    zone = conn.execute(
        "select unverified_data, score_unverified_values, revenue_unverified_values, labels "
        "from serving.zone_card where entity_id = %s",
        (ZONE,),
    ).fetchone()
    assert zone == (False, None, None, [PROPOSED_LABEL, "stale source"])
    assert _one(conn, "select unverified_data from serving.ranked_sites where entity_id = %s", MUN) is False
    assert _one(conn, "select unverified_data from serving.zone_map where entity_id = %s", ZONE) is False


@pytest.mark.db
def test_export_writes_geojson_and_index(tmp_path: Path) -> None:
    url = _url()
    with psycopg.connect(url) as c:
        _cleanup(c)
        try:
            c.execute(
                "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
                "values ('dk', 'Denmark', 'active', 'Europe/Copenhagen', 'DKK', '{}', 't', 't') on conflict do nothing"
            )
            _entity(c, ZONE, "zone", geom=SQUARE)
            _entity(c, S1, "substation", parent=ZONE, geom="POINT(10.05 55.05)")
            c.commit()
            written = [Path(p) for p in export_static(tmp_path, database_url=url)]
        finally:
            _cleanup(c)

    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert {f["path"] for f in index["files"]} | {"index.json"} == {p.name for p in written}
    assert datetime.fromisoformat(index["generated_at"]).tzinfo is not None
    assert isinstance(index["runs"], list) and isinstance(index["labels"], list)
    for f in index["files"]:
        data = json.loads((tmp_path / f["path"]).read_text(encoding="utf-8"))
        if f["format"] == "geojson":
            assert f["view"].removeprefix("serving.") in GEO_COLUMNS
            assert_geojson(data)
            assert len(data["features"]) == f["rows"]
        else:
            assert len(data["rows"]) == f["rows"]
    zones = json.loads((tmp_path / "zone_map.geojson").read_text(encoding="utf-8"))["features"]
    ours = next(z for z in zones if z["id"] == ZONE)
    assert ours["geometry"]["type"] == "Polygon" and "geometry" not in ours["properties"]
    stations = json.loads((tmp_path / "substation_map.geojson").read_text(encoding="utf-8"))["features"]
    assert next(s for s in stations if s["id"] == S1)["properties"]["status_production"] == "unknown"


@pytest.mark.db
def test_report_reads_the_scenario_views_as_anon(tmp_path: Path) -> None:
    """The methods and scenarios report renders from whatever the database serves (scanner.runtime.report)."""
    md, page = (Path(p) for p in write_report(tmp_path, database_url=_url()))
    text = md.read_text(encoding="utf-8")
    assert [line for line in text.splitlines() if line.startswith("## ")] == [
        "## 1. Scenarios",
        "## 2. Scenario comparison",
        "## 3. Assumption register",
        "## 4. Method cards",
    ]
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")


def _cleanup(c: psycopg.Connection[Any]) -> None:
    c.execute("delete from ref.entity_geometry where entity_id in (%s, %s)", (ZONE, S1))
    c.execute("delete from ref.entity where entity_id in (%s, %s)", (S1, ZONE))
    c.commit()
