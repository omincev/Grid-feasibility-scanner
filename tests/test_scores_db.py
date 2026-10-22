"""Scores, opportunity types and BESS v0 against a migrated database. Run with: TEST_DATABASE_URL=... pytest -m db

Inserts minimal reference rows, test assumption sets and two test scenarios (IDs starting with test_), feature
runs with a few feature values, and day-ahead prices. The zones and municipalities scored and valued are
test-only entities standing in for DK1, DK2 and four municipalities (one without stations), listed by a test
repository root, so feature runs and prices the pipeline wrote for real zones never enter the results;
everything the tests and the runs write is removed afterwards. The scenarios are exploratory, so the serving
views of the official scenario are untouched; the tests read the per-scenario helper views and
serving.scenario_comparison.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
from conftest import insert_scenario, score_method_parameters, zones_root
from psycopg.types.json import Jsonb

from scanner.core import db
from scanner.core.registry import PROPOSED_LABEL, MissingAssumption
from scanner.core.time import local_day_bounds
from scanner.runtime.provenance import compute_run
from scanner.scores.run import run_congestion_scores, run_opportunity_types
from scanner.valuation.bess import BessParams, dispatch_day
from scanner.valuation.run import run_bess_v0

pytestmark = pytest.mark.db

CPH = "Europe/Copenhagen"
TAG = "test_scores_db"
DAYS = [local_day_bounds(date(2025, 3, 4) + timedelta(days=i), CPH) for i in range(3)]
WINDOW = (DAYS[0][0], DAYS[-1][1])
F7_PERIOD = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC))
DK1, DK2, DE = "dk.zone.test_scores_dk1", "dk.zone.test_scores_dk2", "de.zone.de_lu"
LOLLAND, RINGKOBING, KBH, SAMSOE = (
    f"dk.municipality.test_scores_{m}" for m in ("lolland", "ringkoebing", "koebenhavn", "samsoe")
)
TEST_ENTITIES = [LOLLAND, RINGKOBING, KBH, SAMSOE, DK1, DK2]  # in deletion order; DE is a real zone and stays
SPEC, DEMO = "test_scores_spec", "test_scores_demo"  # like spec_v0 and demo_default

WEIGHTS = {
    "f7_substation_saturation": 20,
    "f1_price_spread": 15,
    "f2_curtailment": 15,
    "f4_surplus": 15,
    "f5_balancing_activation": 15,
    "f3_interconnector_saturation": 10,
    "f6_renewable_pipeline": 10,
}
RULES = {
    "bess": {
        "high": ["f1_price_spread", "f5_balancing_activation"],
        "substation_not_saturated": "production_or_consumption",
    },
    "dsf": {"high": ["f5_balancing_activation"], "requires": ["industrial_load_nearby"]},
    "modular_dc": {
        "high": ["f2_curtailment", "f4_surplus"],
        "substation": {"production": "saturated", "consumption": "not_saturated"},
    },
}
SETS = {
    "test_score_weights": {
        "weights": {"value": None, "proposed": WEIGHTS},  # proposed only: needs allow_proposed
        "missing_feature_policy": {"value": "renormalise"},
        "percentile_population": {"value": "pooled_daily_values_reference_zones"},
    },
    "test_score_method": score_method_parameters(),
    "test_opportunity_rules": {"high_percentile": {"value": 70}, "rules": {"value": RULES}},
    "test_bess": {
        "power_mw": {"value": 1.0},
        "duration_h": {"value": 2.0},
        "round_trip_efficiency": {"value": None, "proposed": 0.81},
        "charge_efficiency": {"value": None},
        "discharge_efficiency": {"value": None},
        "soc_min_fraction": {"value": 0.0},
        "soc_max_fraction": {"value": 1.0},
        "soc_initial_fraction": {"value": 0.0},
        "max_cycles_per_day": {"value": 1.0},
    },
}
ROLES = {
    "score_weights": "test_score_weights",
    "score_method": "test_score_method",
    "opportunity_rules": "test_opportunity_rules",
    "bess_template": "test_bess",
}
DEMO_SWITCHES = {  # demo_default's method switches
    "score_method.zone_inputs": ["f1_price_spread", "f2_curtailment", "f4_surplus", "f5_balancing_activation"],
    "score_method.municipality_missing_input_policy": "null_component",
}
PRICE_ENTRIES = {
    "dk.eds.Elspotprices.SpotPriceEUR": timedelta(hours=1),
    "dk.eds.DayAheadPrices.DayAheadPriceEUR": timedelta(minutes=15),
}


@pytest.fixture()
def url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("GIT_SHA", "testsha")
    with psycopg.connect(url) as conn:
        _cleanup(conn)
        _reference(conn)
        conn.commit()
    yield url
    with psycopg.connect(url) as conn:
        _cleanup(conn)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """Denmark with the two test zones only (the runs score and value nothing else)."""
    return zones_root(tmp_path, [DK1, DK2])


def _cleanup(conn: psycopg.Connection[Any]) -> None:
    runs = """
        select run_id from ops.compute_run
        where params->>'test' = %(tag)s or params->>'weights_set' like 'test\\_%%'
           or params->>'rules_set' like 'test\\_%%' or params->>'template_set' like 'test\\_%%'
    """
    p = {"tag": TAG}
    conn.execute(f"delete from feat.opportunity where run_id in ({runs})", p)
    conn.execute(f"delete from feat.score where run_id in ({runs})", p)
    valuations = f"select valuation_run_id from val.run where run_id in ({runs})"
    conn.execute(f"delete from val.result where valuation_run_id in ({valuations})", p)
    conn.execute(f"delete from val.run where run_id in ({runs})", p)
    conn.execute(f"delete from feat.feature_value where run_id in ({runs})", p)
    conn.execute(f"delete from ops.compute_run where run_id in ({runs})", p)
    conn.execute("delete from ref.scenario where scenario_id like 'test\\_scores\\_%'")
    conn.execute("delete from ref.assumption_set where set_id like 'test\\_%'")
    conn.execute(
        "delete from core.observation where series_id in (select series_id from core.series where series_key like %s)",
        (f"{TAG}.%",),
    )
    conn.execute("delete from core.series where series_key like %s", (f"{TAG}.%",))
    for entity_id in TEST_ENTITIES:
        conn.execute("delete from ref.entity where entity_id = %s", (entity_id,))
    conn.commit()


def _reference(conn: psycopg.Connection[Any]) -> None:
    for code, status in (("dk", "active"), ("de", "reference")):
        conn.execute(
            "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
            "values (%s, %s, %s, 'Europe/Copenhagen', 'EUR', '{}', 't', 't') on conflict do nothing",
            (code, code.upper(), status),
        )
    entities = [(DK1, "zone", "dk", None), (DK2, "zone", "dk", None), (DE, "zone", "de", None)]
    entities += [(LOLLAND, "municipality", "dk", DK2), (RINGKOBING, "municipality", "dk", DK1)]
    entities += [(KBH, "municipality", "dk", DK2), (SAMSOE, "municipality", "dk", DK1)]
    for entity_id, kind, country, parent in entities:
        conn.execute(
            "insert into ref.entity (entity_id, kind, country_code, parent_id, name) values (%s, %s, %s, %s, %s) "
            "on conflict do nothing",
            (entity_id, kind, country, parent, entity_id),
        )
    for set_id, params in SETS.items():
        conn.execute(
            "insert into ref.assumption_set (set_id, kind, status, owners, parameters, source_path, content_hash) "
            "values (%s, 'test', 'proposed', '{owner}', %s, 'test', %s)",
            (set_id, Jsonb(params), f"hash-{set_id}"),
        )
    insert_scenario(conn, SPEC, ROLES)
    insert_scenario(conn, DEMO, ROLES, method_switches=DEMO_SWITCHES, approved=True)
    conn.execute("insert into ref.equivalence_class values ('day_ahead_price', 'x', 'x') on conflict do nothing")
    for entry_id in PRICE_ENTRIES:
        _, source, dataset, field = entry_id.split(".")
        conn.execute(
            "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, equivalence_class, "
            "kind, unit, resample, definition, status, source_path, content_hash) "
            "values (%s, %s, %s, 'dk', %s, 'day_ahead_price', 'actual', 'EUR/MWh', 'mean', '{}', 'draft', 't', 't') "
            "on conflict do nothing",
            (entry_id, source, dataset, field),
        )


Row = tuple[str, str, datetime, datetime, str, float]


def _feature_run(conn: psycopg.Connection[Any], name: str, rows: list[Row], unverified: int | None = None) -> str:
    with compute_run(conn, kind="feature", name=name, params={"test": TAG}) as run:
        if unverified is not None:  # as scanner.features.run records it
            run.params["unverified_values"] = unverified
        conn.cursor().executemany(
            "insert into feat.feature_value (feature_key, entity_id, period_start, period_end, statistic, value, "
            "run_id, code_version) values (%s, %s, %s, %s, %s, %s, %s, 'testsha')",
            [(*r, run.run_id) for r in rows],
        )
    return str(run.run_id)


def _daily(key: str, entity: str, stat: str, values: list[float], window_stat: str) -> list[Row]:
    rows: list[Row] = [(key, entity, s, e, stat, v) for (s, e), v in zip(DAYS, values, strict=True)]
    rows.append((key, entity, *WINDOW, window_stat, sum(values) / len(values)))
    return rows


def _insert_features(conn: psycopg.Connection[Any]) -> None:
    f1 = "f1_intraday_spread_hourly"
    rows = _daily(f1, DK1, "daily_spread", [10, 20, 30], "mean") + _daily(f1, DK2, "daily_spread", [40, 50, 60], "mean")
    rows += _daily(f1, DE, "daily_spread", [0, 5, 100], "mean")
    rows.append((f1, DK1, DAYS[0][0], DAYS[1][1], "mean", 999.0))  # a sub-window: never the raw value
    _feature_run(conn, "f1_price_spread", rows, unverified=5)
    f5 = "f5_imbalance_spot_spread"
    _feature_run(
        conn,
        "f5_balancing_activation",
        _daily(f5, DK1, "daily_mean", [1, 2, 3], "mean") + _daily(f5, DK2, "daily_mean", [4, 5, 6], "mean"),
    )
    f7 = "f7_substation_saturation"
    # share without free production, share without free consumption, stations (all assessed in both
    # directions), share with free consumption and no free production (the load-type statistic)
    shares = {
        DK1: (0.2, 0.0, 10.0, 0.2),
        DK2: (0.6, 1.0, 5.0, 0.0),
        LOLLAND: (1.0, 0.5, 2.0, 0.5),
        RINGKOBING: (0.5, 0.0, 4.0, 0.5),
        KBH: (0.0, 1.0, 3.0, 0.0),
    }
    rows = []
    for entity, (prod, cons, n, load) in shares.items():
        rows += [
            (f7, entity, *F7_PERIOD, "share_stations_no_free_production", prod),
            (f7, entity, *F7_PERIOD, "share_stations_no_free_consumption", cons),
            (f7, entity, *F7_PERIOD, "stations", n),
            (f7, entity, *F7_PERIOD, "stations_assessed_production", n),
            (f7, entity, *F7_PERIOD, "stations_assessed_both", n),
            (f7, entity, *F7_PERIOD, "share_stations_free_consumption_no_free_production", load),
        ]
    _feature_run(conn, f7, rows)


def _scores(conn: psycopg.Connection[Any], run_id: str) -> dict[str, tuple[Any, ...]]:
    return {
        e: (v, b, s, ps, pe, w)
        for e, v, b, s, ps, pe, w in conn.execute(
            "select entity_id, value, breakdown, sensitivity::text, period_start, period_end, weights_set_id "
            "from feat.score where run_id = %s",
            (uuid.UUID(run_id),),
        )
    }


def test_scores_and_opportunities(url: str, root: Path) -> None:
    with psycopg.connect(url) as conn:
        _insert_features(conn)
        with pytest.raises(MissingAssumption):  # weights are only proposed
            run_congestion_scores(database_url=url, root=root, scenario=SPEC, allow_proposed=False)
        failed = conn.execute(
            "select status, error, scenario_id from ops.compute_run where params->>'weights_set' = 'test_score_weights'"
        ).fetchall()
        assert failed[0][0] == "failed" and "owner" in failed[0][1] and failed[0][2] == SPEC

        out = run_congestion_scores(database_url=url, root=root, scenario=SPEC, allow_proposed=True)
        assert out.status == "partial" and any("f4_surplus_index" in n for n in out.notes)
        assert out.scenario_id == SPEC
        scores = _scores(conn, out.run_id)
        assert {DK1, DK2, LOLLAND, RINGKOBING, KBH, SAMSOE} <= set(scores) and DE not in scores
        columns = conn.execute(
            "select entity_id, scenario_id, scenario_hash, assumption_hashes, method_id, method_version, "
            "method_versions from feat.score where run_id = %s and entity_id in (%s, %s) order by entity_id",
            (uuid.UUID(out.run_id), DK1, LOLLAND),
        ).fetchall()
        hashes = {"test_score_weights": "hash-test_score_weights", "test_score_method": "hash-test_score_method"}
        features = {"f1_price_spread": None, "f5_balancing_activation": None, "f7_substation_saturation": None}
        assert columns == [
            (
                LOLLAND,
                SPEC,
                f"hash-{SPEC}",
                hashes,
                "municipality_site_score",
                "2",
                {**features, "municipality_site_score": 2},
            ),
            (DK1, SPEC, f"hash-{SPEC}", hashes, "congestion_score", "1", {**features, "congestion_score": 1}),
        ]

        # DK1: F1 mean 20 in pooled daily spreads {10,20,30,40,50,60,0,5,100} -> 3.5/9; F5 mean 2 in
        # {1..6} -> 1.5/6; F7 share 0.2 among zones {0.2, 0.6} -> 25. Weights 15/15/20 renormalised to 30/30/40.
        value, b, sensitivity, start, end, weights_set = scores[DK1]
        p1, p5, p7 = 100 * 3.5 / 9, 25.0, 25.0
        assert value == pytest.approx(0.3 * p1 + 0.3 * p5 + 0.4 * p7)
        assert (sensitivity, weights_set, (start, end)) == ("public", "test_score_weights", WINDOW)
        f1 = b["features"]["f1_intraday_spread_hourly"]
        assert (f1["raw"], f1["percentile"], f1["weight"]) == (20.0, pytest.approx(p1), pytest.approx(30.0))
        assert f1["population"]["n"] == 9 and f1["population"]["entities"] == sorted([DE, DK1, DK2])
        assert b["features"]["f7_substation_saturation"]["population"]["method"] == "peer_areas"
        assert set(b["features"]) == {
            "f1_intraday_spread_hourly",
            "f5_imbalance_spot_spread",
            "f7_substation_saturation",
        }
        assert b["missing_features"] == sorted(
            k for k in WEIGHTS if k not in ("f1_price_spread", "f5_balancing_activation", "f7_substation_saturation")
        )
        assert b["assumption_sets"] == [
            {
                "set_id": "test_score_weights",
                "content_hash": "hash-test_score_weights",
                "proposed_used": True,
                "proposed_parameters": ["weights"],
                "role": "score_weights",
                "scenario_parameters": [],
            },
            {
                "set_id": "test_score_method",
                "content_hash": "hash-test_score_method",
                "proposed_used": False,
                "proposed_parameters": [],
                "role": "score_method",
                "scenario_parameters": [],
            },
        ]
        assert b["scenario"] == {
            "scenario_id": SPEC,
            "content_hash": f"hash-{SPEC}",
            "status": "exploratory",
            "approved": False,
        }
        assert b["method"] == {"method_id": "congestion_score", "version": 1}
        assert b["proposed_assumptions"] is True and b["labels"] == [PROPOSED_LABEL]
        assert b["window"] == [WINDOW[0].isoformat(), WINDOW[1].isoformat()]
        assert b["capacity"]["share_stations_no_free_consumption"] == 0.0
        # unverified input values per feature run behind the score: 5 in the F1 run, none recorded by the others
        assert sorted(b["unverified_inputs"].values(), key=str) == [5, None, None] and b["unverified_values"] == 5

        # Lolland inherits DK2's F1 and F5 and ranks its own F7 share 1.0 among {1.0, 0.5, 0.0}.
        value, b, *_ = scores[LOLLAND]
        dk2 = scores[DK2][1]["features"]
        assert (
            b["features"]["f1_intraday_spread_hourly"]["percentile"] == dk2["f1_intraday_spread_hourly"]["percentile"]
        )
        assert b["features"]["f1_intraday_spread_hourly"]["inherited_from"] == DK2
        assert b["features"]["f7_substation_saturation"]["percentile"] == pytest.approx(250 / 3)
        assert b["features"]["f7_substation_saturation"]["population"]["level"] == "municipality"
        assert (b["level"], b["parent_zone"]) == ("municipality", DK2)
        assert value == pytest.approx(0.3 * 100 * 6.5 / 9 + 0.3 * 75 + 0.4 * 250 / 3)
        # Samsoe has no stations: renormalised over DK1's inherited F1 and F5, flagged
        value, b, *_ = scores[SAMSOE]
        assert value == pytest.approx(0.5 * 100 * 3.5 / 9 + 0.5 * 25)
        assert "f7_substation_saturation" not in b["features"]
        assert b["flags"] == ["no_feature_data", "no_station_data"]  # F2 and F4 have no data in this test
        assert b["missing_inputs"]["f7_substation_saturation"] == "no_station_data"

        breakdown = conn.execute(
            "select feature_key, raw_value, percentile, weight, contribution from feat.scenario_score_component "
            "where scenario_id = %s and entity_id = %s and run_id = %s order by feature_key",
            (SPEC, DK1, uuid.UUID(out.run_id)),
        ).fetchall()
        assert [r[0] for r in breakdown] == sorted(scores[DK1][1]["features"])
        assert sum(r[4] for r in breakdown) == pytest.approx(scores[DK1][0])
        site = conn.execute(
            "select congestion_score, score_run_id from serving.scenario_comparison "
            "where entity_id = %s and scenario_id = %s",
            (LOLLAND, SPEC),
        )
        assert site.fetchone() == (pytest.approx(scores[LOLLAND][0]), uuid.UUID(out.run_id))

        with pytest.raises(ValueError, match="not a run of scenario"):
            run_opportunity_types(database_url=url, scenario=DEMO, score_run_id=out.run_id)
        opp = run_opportunity_types(database_url=url, scenario=SPEC, score_run_id=out.run_id)
        assert opp.status == "ok" and opp.rows_written == 3 * len(scores)
        rows = {
            (e, a): (m, s, r)
            for e, a, m, s, r in conn.execute(
                "select entity_id, asset_type, matched, status, reasons from feat.opportunity where run_id = %s",
                (uuid.UUID(opp.run_id),),
            )
        }
        assert rows[DK2, "bess"][:2] == (True, "match")  # F1 72.2, F5 75 >= 70; production room at DK2
        assert rows[DK1, "bess"][:2] == (False, "no_match")  # F1 38.9
        assert rows[DK2, "dsf"][1] == "candidate_needs_site_data"
        assert rows[DK2, "modular_dc"][1] == "no_match"  # no F2 or F4
        assert rows[LOLLAND, "bess"][1] == "match"  # production saturated, consumption has room
        reasons = rows[DK2, "bess"][2]
        assert [c["condition"] for c in reasons["conditions"]] == [
            "high_percentile",
            "high_percentile",
            "substation_not_saturated_any",
        ]
        assert reasons["conditions"][0]["threshold"] == 70 and reasons["score_run_id"] == out.run_id
        assert reasons["assumption_sets"][0]["set_id"] == "test_opportunity_rules"
        assert reasons["proposed_assumptions"] is True and reasons["labels"] == [PROPOSED_LABEL]  # via the weights
        assert reasons["unverified_values"] == 5
        opp_columns = conn.execute(
            "select scenario_id, assumption_hashes, method_id, method_versions from feat.opportunity "
            "where run_id = %s and entity_id = %s and asset_type = 'bess'",
            (uuid.UUID(opp.run_id), DK2),
        ).fetchone()
        assert opp_columns is not None
        assert opp_columns[0] == SPEC and opp_columns[2] == "opportunity_rules"
        assert opp_columns[1] == {**hashes, "test_opportunity_rules": "hash-test_opportunity_rules"}
        assert opp_columns[3]["opportunity_rules"] == 1 and opp_columns[3]["congestion_score"] == 1
        view = conn.execute(
            "select status from feat.scenario_opportunity where scenario_id = %s and entity_id = %s "
            "and asset_type = 'bess'",
            (SPEC, DK2),
        )
        assert view.fetchone() == ("match",)


def test_demo_switches_leave_f7_out_of_zones_and_null_for_sites_without_stations(url: str, root: Path) -> None:
    """demo_default's method switches: F7 out of the zone score, kept for sites; no stations = null F7."""
    with psycopg.connect(url) as conn:
        _insert_features(conn)
        spec = _scores(
            conn, run_congestion_scores(database_url=url, root=root, scenario=SPEC, allow_proposed=True).run_id
        )
        out = run_congestion_scores(database_url=url, root=root, scenario=DEMO, allow_proposed=True)
        demo = _scores(conn, out.run_id)
        p1 = {DK1: 100 * 3.5 / 9, DK2: 100 * 6.5 / 9}
        p5 = {DK1: 25.0, DK2: 75.0}

        value, b, *_ = demo[DK1]  # F1 and F5 only (F2 and F4 have no data), renormalised
        assert value == pytest.approx(0.5 * p1[DK1] + 0.5 * p5[DK1])
        assert set(b["features"]) == {"f1_intraday_spread_hourly", "f5_imbalance_spot_spread"}
        assert b["capacity"]["share_stations_no_free_production"] == 0.2  # still there for the opportunity rules
        sets = {s["set_id"]: s for s in b["assumption_sets"]}
        assert sets["test_score_method"]["scenario_parameters"] == ["municipality_missing_input_policy", "zone_inputs"]
        assert not sets["test_score_method"]["proposed_used"]  # the demo test scenario is approved

        # Lolland keeps its F7; the weights are spread over all five municipality inputs (60 + 20 = 80 -> 100)
        value, b, *_ = demo[LOLLAND]
        assert value == pytest.approx(0.1875 * p1[DK2] + 0.1875 * p5[DK2] + 0.25 * 250 / 3)
        assert b["features"]["f2_nonpositive_price_share"]["flag"] == "no_feature_data"
        assert b["missing_input_policy"] == "null_component"

        value, b, *_ = demo[SAMSOE]  # no stations: F7 null and flagged, not renormalised away
        f7 = b["features"]["f7_substation_saturation"]
        assert (f7["raw"], f7["percentile"], f7["contribution"], f7["flag"]) == (None, None, None, "no_station_data")
        assert f7["weight"] == pytest.approx(25.0)
        assert value == pytest.approx(0.1875 * p1[DK1] + 0.1875 * p5[DK1])
        assert value < spec[SAMSOE][0] and b["flags"] == ["no_feature_data", "no_station_data"]

        flags = conn.execute(
            "select flag from feat.scenario_score_component where scenario_id = %s and entity_id = %s "
            "and feature_key = 'f7_substation_saturation'",
            (DEMO, SAMSOE),
        ).fetchone()
        assert flags == ("no_station_data",)
        compared = conn.execute(
            "select scenario_id, congestion_score, rank, score_flags from serving.scenario_comparison "
            "where entity_id = %s and scenario_id in (%s, %s) order by scenario_id",
            (SAMSOE, DEMO, SPEC),
        ).fetchall()
        assert [(s, pytest.approx(v)) for s, v, *_ in compared] == [
            (DEMO, demo[SAMSOE][0]),
            (SPEC, spec[SAMSOE][0]),
        ]
        assert compared[0][3] == ["no_feature_data", "no_station_data"]


def test_direction_aware_site_scores_flag_few_stations(url: str, root: Path) -> None:
    """f7_direction_aware's switch: load-type site scores read the load-type F7 statistic; zones are unchanged;
    a municipality below the minimum station count is flagged low_station_count and still scored and ranked."""
    scenario = "test_scores_direction"
    with psycopg.connect(url) as conn:
        _insert_features(conn)
        switches = {**DEMO_SWITCHES, "score_method.municipality_grid_direction": "load"}
        insert_scenario(conn, scenario, ROLES, method_switches=switches, approved=True)
        conn.commit()
        demo = _scores(
            conn, run_congestion_scores(database_url=url, root=root, scenario=DEMO, allow_proposed=True).run_id
        )
        out = run_congestion_scores(database_url=url, root=root, scenario=scenario, allow_proposed=True)
        load = _scores(conn, out.run_id)
        assert load[DK1][0] == pytest.approx(demo[DK1][0])  # zones: no F7 either way
        statistic = "share_stations_free_consumption_no_free_production"
        p1, p5 = 100 * 6.5 / 9, 75.0  # DK2's inherited percentiles

        value, b, *_ = load[LOLLAND]  # load share 0.5 among {0.5, 0.5, 0.0}; 2 stations
        f7 = b["features"]["f7_substation_saturation"]
        assert (f7["statistic"], f7["raw"], f7["percentile"]) == (statistic, 0.5, pytest.approx(200 / 3))
        assert value == pytest.approx(0.1875 * p1 + 0.1875 * p5 + 0.25 * 200 / 3)
        assert b["flags"] == ["low_station_count", "no_feature_data"]
        assert b["station_sample"] == {
            "f7_substation_saturation": {"statistic": "stations_assessed_both", "stations": 2.0, "min_stations": 3}
        }
        assert b["site"] == {"grid_direction": "load", "asset_types": ["bess", "dsf", "modular_dc"]}
        assert "low_station_count" in demo[LOLLAND][1]["flags"]  # also by its production count in demo_default
        assert demo[LOLLAND][1]["site"]["grid_direction"] == "any"

        _, b, *_ = load[KBH]  # 3 stations: not flagged
        assert b["flags"] == ["no_feature_data"] and b["station_sample"]["f7_substation_saturation"]["stations"] == 3
        assert load[SAMSOE][1]["flags"] == ["no_feature_data", "no_station_data"]
        params = conn.execute("select params from ops.compute_run where run_id = %s", (uuid.UUID(out.run_id),))
        (run_params,) = params.fetchone() or ({},)
        assert run_params["municipality_grid_direction"] == "load"
        assert {si["statistic"] for si in run_params["inputs"]["municipality"]} >= {statistic}
        compared = conn.execute(
            "select rank, score_flags from serving.scenario_comparison where entity_id = %s and scenario_id = %s",
            (LOLLAND, scenario),
        ).fetchone()
        assert compared is not None and compared[1] == ["low_station_count", "no_feature_data"]


def _write_prices(
    conn: psycopg.Connection[Any], entry_id: str, start: datetime, values: list[float | None], quality: str = "ok"
) -> uuid.UUID:
    step = PRICE_ENTRIES[entry_id]
    key = f"{TAG}.dk1.{entry_id}"
    sid = db.ensure_series(conn, db.SeriesSpec(key, entry_id, DK1, "EUR/MWh", step, "actual", "public", None))
    rows = [
        db.ObservationRow(key, start + i * step, start + (i + 1) * step, None, v, quality) for i, v in enumerate(values)
    ]
    run_id = uuid.uuid4()
    db.write_observations(conn, rows, {key: sid}, run_id)
    conn.commit()
    return run_id


def test_bess_return_range(url: str, root: Path) -> None:
    sep30 = local_day_bounds(date(2025, 9, 30), CPH)[0]
    oct1 = local_day_bounds(date(2025, 10, 1), CPH)[0]
    oct2 = local_day_bounds(date(2025, 10, 2), CPH)[0]
    hourly = [0.0] * 12 + [100.0] * 12
    quarter = [50.0] * 40 + [150.0] + [50.0] * 55
    with psycopg.connect(url) as conn:
        _write_prices(conn, "dk.eds.Elspotprices.SpotPriceEUR", sep30, [*hourly, 999.0])  # 999: hourly after the switch
        # quarantined (a soft fingerprint deviation), not rejected: used and counted as unverified
        unverified_run = _write_prices(conn, "dk.eds.DayAheadPrices.DayAheadPriceEUR", oct1, quarter, "quarantined")
        _write_prices(conn, "dk.eds.DayAheadPrices.DayAheadPriceEUR", oct2 + timedelta(minutes=15), [60.0] * 95)

        out = run_bess_v0(database_url=url, end_day=date(2025, 10, 3), root=root, scenario=SPEC, allow_proposed=True)
        assert out.status == "partial" and out.rows_written == 1  # only DK1 has prices
        assert any(n.startswith(f"{DK2}: no day-ahead prices") for n in out.notes)
        (vid, asset, template, start, end, inputs, labels, sensitivity, *scenario_columns), *more = conn.execute(
            "select valuation_run_id, asset_type, template_set_id, price_window_start, price_window_end, inputs, "
            "labels, sensitivity::text, scenario_id, scenario_hash, assumption_hashes, method, method_version, "
            "method_versions from val.run where run_id = %s",
            (uuid.UUID(out.run_id),),
        ).fetchall()
        assert not more
        assert (asset, template, sensitivity) == ("bess", "test_bess", "public")
        method = "bess_v0_daily_lp_day_ahead"
        expected = [SPEC, f"hash-{SPEC}", {"test_bess": "hash-test_bess"}, method, "1", {method: 1}]
        assert scenario_columns == expected
        result_columns = conn.execute(
            "select scenario_id, scenario_hash, assumption_hashes, method_id, method_version, method_versions "
            "from val.result where valuation_run_id = %s",
            (vid,),
        ).fetchone()
        assert list(result_columns or ()) == expected
        assert (start, end) == (local_day_bounds(date(2025, 3, 4), CPH)[0], local_day_bounds(date(2025, 10, 3), CPH)[0])
        days_in_window = (date(2025, 10, 3) - date(2025, 3, 4)).days
        assert (inputs["days_in_window"], inputs["days_valued"]) == (days_in_window, 2)
        assert inputs["skipped_days"] == days_in_window - 2
        assert inputs["skipped_day_ranges"] == [["2025-03-04", "2025-09-29"], ["2025-10-02", "2025-10-02"]]
        assert inputs["price_series"] == sorted(f"{TAG}.dk1.{e}" for e in PRICE_ENTRIES)
        assert (inputs["unverified_values"], inputs["unverified_ingest_runs"]) == (96, [str(unverified_run)])
        params = conn.execute("select params from ops.compute_run where run_id = %s", (uuid.UUID(out.run_id),))
        assert params.fetchone()[0]["unverified_values"] == 96  # type: ignore[index]
        assert inputs["assumption_sets"][0] == {
            "set_id": "test_bess",
            "content_hash": "hash-test_bess",
            "proposed_used": True,
            "proposed_parameters": ["round_trip_efficiency"],
            "role": "bess_template",
            "scenario_parameters": [],
        }
        assert inputs["scenario"]["scenario_id"] == SPEC and inputs["method"]["method_id"] == method
        bias = ["zone-level", "perfect-foresight upper bound", "day-ahead arbitrage only"]
        assert labels == [*bias, "less than one year of data", PROPOSED_LABEL]

        p = BessParams.from_round_trip(
            power_mw=1.0,
            duration_h=2.0,
            round_trip_efficiency=0.81,
            soc_min_fraction=0.0,
            soc_max_fraction=1.0,
            soc_initial_fraction=0.0,
            max_cycles_per_day=1.0,
        )
        rev = dispatch_day(np.array(hourly), 1.0, p).revenue_eur + dispatch_day(np.array(quarter), 0.25, p).revenue_eur
        assert dispatch_day(np.array(hourly), 1.0, p).revenue_eur == pytest.approx(180.0)  # 2 MWh stored, 1.8 sold
        result = conn.execute(
            "select metric, low, mid, high, unit from val.result where valuation_run_id = %s", (vid,)
        ).fetchone()
        assert result == (
            "revenue_eur_per_mw_year",
            pytest.approx(rev / 2 * 365),
            None,
            pytest.approx(rev / 2 * 365),
            "EUR/MW/year",
        )
        served = conn.execute(
            "select low, high, labels from feat.scenario_return_range where scenario_id = %s and entity_id = %s "
            "and asset_type = 'bess'",
            (SPEC, DK1),
        ).fetchone()
        assert served is not None and served[0] == pytest.approx(rev / 2 * 365)

        with pytest.raises(MissingAssumption):
            run_bess_v0(database_url=url, end_day=date(2025, 10, 3), root=root, scenario=SPEC, allow_proposed=False)


def test_score_and_valuation_runs_need_only_the_analytics_role(url: str, root: Path, analytics_url: str) -> None:
    """The analytics location may connect as a login role in scanner_analytics (ANALYTICS_DATABASE_URL)."""
    with psycopg.connect(url) as conn:
        _insert_features(conn)
        oct1 = local_day_bounds(date(2025, 10, 1), CPH)[0]
        _write_prices(conn, "dk.eds.DayAheadPrices.DayAheadPriceEUR", oct1, [50.0] * 40 + [150.0] + [50.0] * 55)
    scores = run_congestion_scores(database_url=analytics_url, root=root, scenario=SPEC, allow_proposed=True)
    opportunities = run_opportunity_types(database_url=analytics_url, scenario=SPEC, allow_proposed=True)
    valuation = run_bess_v0(
        database_url=analytics_url, end_day=date(2025, 10, 3), root=root, scenario=SPEC, allow_proposed=True
    )
    for o in (scores, opportunities, valuation):
        assert o.status in ("ok", "partial") and o.rows_written > 0, o
