"""F7 substation saturation (scanner.features.substation) on synthetic station facts: the binding value
(lower of with and without the TSO transformer), nulls, TSO consumption not assessed, ties in the zone rank,
levels kept apart, zone and municipality aggregates over DSO stations and over DSO and TSO stations, the
MW means and direction-aware statistics, their sample counts, and combined sensitivity."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import polars as pl
import pytest

from scanner import REPO_ROOT
from scanner.features import substation
from scanner.features.registry import FEATURE_INPUTS, FEATURES

PERIOD = (datetime(2025, 3, 3, 23, tzinfo=UTC), datetime(2026, 10, 17, 22, tzinfo=UTC))
SCHEMA: dict[str, Any] = {
    "entity_id": pl.String,
    "zone": pl.String,
    "municipality": pl.String,
    "role": pl.String,
    "value": pl.Float64,
    "sensitivity": pl.String,
}


Row = tuple[Any, ...]


def dso(eid: str, zone: str | None, mun: str | None, cons: Row, prod: Row, sens: str = "public") -> list[Row]:
    roles = ("dso_consumption_no_tso", "dso_consumption_with_tso", "dso_production_no_tso", "dso_production_with_tso")
    return [(eid, zone, mun, r, v, sens) for r, v in zip(roles, (*cons, *prod), strict=True)]


def tso(eid: str, zone: str, cons: float, prod: float) -> list[Row]:
    return [(eid, zone, None, "tso_consumption", cons, "public"), (eid, zone, None, "tso_production", prod, "public")]


FACTS = pl.DataFrame(
    [
        *dso("a", "dk.zone.dk1", "m.x", (60.0, 0.0), (20.0, 40.0)),  # binding: cons 0, prod 20
        *dso("b", "dk.zone.dk1", "m.x", (0.0, 0.0), (100.0, 100.0)),
        *dso("c", "dk.zone.dk1", "m.y", (None, None), (20.0, 60.0)),  # consumption not provided
        *dso("d", "dk.zone.dk1", "m.y", (30.0, 50.0), (0.0, 20.0)),
        *dso("e", "dk.zone.dk2", "m.z", (10.0, 20.0), (40.0, None), "licensed"),  # production unknown
        *dso("u", None, None, (0.0, 0.0), (0.0, 0.0)),  # no zone, no municipality
        *tso("t1", "dk.zone.dk1", 0.0, 385.0),
        *tso("t2", "dk.zone.dk1", 0.0, 42.0),
    ],
    schema=SCHEMA,
    orient="row",
)


def stats(rows: pl.DataFrame, entity: str) -> dict[str, float]:
    return dict(rows.filter(pl.col("entity_id") == entity).select("statistic", "value").iter_rows())


@pytest.fixture(scope="module")
def rows() -> pl.DataFrame:
    return substation.f7_rows(FACTS, PERIOD)


def test_binding_free_mw_is_the_lower_value_and_null_stays_out(rows: pl.DataFrame) -> None:
    a, c, e = stats(rows, "a"), stats(rows, "c"), stats(rows, "e")
    assert (a["free_mw_consumption"], a["free_mw_production"]) == (0.0, 20.0)
    assert "free_mw_consumption" not in c and "rank_in_zone_consumption" not in c and c["free_mw_production"] == 20.0
    assert "free_mw_production" not in e and e["free_mw_consumption"] == 10.0  # one of the two is null


def test_rank_in_zone_ties_share_the_lowest_rank_and_levels_stay_apart(rows: pl.DataFrame) -> None:
    rank = {e: stats(rows, e).get("rank_in_zone_consumption") for e in "abcdu"}
    assert rank == {"a": 1.0, "b": 1.0, "c": None, "d": 3.0, "u": None}  # 0, 0, -, 30
    production = {e: stats(rows, e)["rank_in_zone_production"] for e in "abcd"}  # 20, 100, 20, 0
    assert production == {"a": 2.0, "b": 4.0, "c": 2.0, "d": 1.0}
    assert stats(rows, "e")["rank_in_zone_consumption"] == 1.0  # alone among DK2 DSO stations
    assert stats(rows, "t1")["rank_in_zone_production"] == 2.0 and stats(rows, "t2")["rank_in_zone_production"] == 1.0
    assert stats(rows, "t1")["free_mw_production"] == 385.0  # TSO: the existing-grid value as published


def test_tso_consumption_is_not_assessed(rows: pl.DataFrame) -> None:
    """The published TSO consumption 0 MW is neither a value, nor a rank, nor a station without capacity."""
    assert set(substation.NOT_ASSESSED) == {"tso_consumption"}
    for t in ("t1", "t2"):
        assert set(stats(rows, t)) == {"free_mw_production", "rank_in_zone_production"}
    dk1 = stats(rows, "dk.zone.dk1")
    assert dk1["stations_incl_tso"] == 6.0 and dk1["stations_assessed_consumption_incl_tso"] == 3.0
    assert dk1["share_stations_no_free_consumption_incl_tso"] == dk1["share_stations_no_free_consumption"]
    assert dk1["stations_assessed_both_incl_tso"] == dk1["stations_assessed_both"] == 3.0
    published = FACTS.filter(pl.col("role") == "tso_consumption")
    assert published["value"].to_list() == [0.0, 0.0]  # the input keeps the facts as published


def test_tso_inclusive_statistics_add_existing_grid_production(rows: pl.DataFrame) -> None:
    dk1 = stats(rows, "dk.zone.dk1")
    assert dk1["stations_assessed_production_incl_tso"] == 6.0  # a, b, c, d and t1, t2
    assert dk1["share_stations_no_free_production_incl_tso"] == pytest.approx(1 / 6)  # d
    assert dk1["free_mw_production_mean_incl_tso"] == pytest.approx((20 + 100 + 20 + 0 + 385 + 42) / 6)
    assert "free_mw_production_sum_incl_tso" not in dk1  # sums stay DSO only
    assert stats(rows, "m.x")["stations_incl_tso"] == 2.0  # TSO stations without a municipality stay out


def test_mw_means_and_direction_aware_statistics(rows: pl.DataFrame) -> None:
    # DK1 DSO stations (consumption, production): a (0, 20), b (0, 100), c (-, 20), d (30, 0)
    dk1 = stats(rows, "dk.zone.dk1")
    assert (dk1["stations_assessed_consumption"], dk1["stations_assessed_production"]) == (3.0, 4.0)
    assert (dk1["free_mw_consumption_mean"], dk1["free_mw_production_mean"]) == (10.0, 35.0)
    assert dk1["stations_assessed_both"] == 3.0  # c has no consumption value
    assert dk1["share_stations_free_consumption_no_free_production"] == pytest.approx(1 / 3)  # d
    assert dk1["share_stations_free_production_no_free_consumption"] == pytest.approx(2 / 3)  # a, b
    assert dk1["free_mw_consumption_mean_no_free_production"] == pytest.approx(30 / 3)  # d's 30 MW
    assert dk1["free_mw_production_mean_no_free_consumption"] == pytest.approx((20 + 100) / 3)
    dk2 = stats(rows, "dk.zone.dk2")  # e: consumption 10, production unknown
    assert (dk2["stations_assessed_both"], dk2["stations_assessed_production"]) == (0.0, 0.0)
    assert not [s for s in dk2 if "no_free_production" in s or s.startswith("free_mw_production")]
    for statistic, count in substation.SAMPLE_STATISTIC.items():
        assert count is None or count in dk1, statistic


def test_limited_access_ranges_are_never_read() -> None:
    """Inverted ranges (upper bound 0) stay as published: no F7 input reads a limited-access class."""
    inputs = FEATURE_INPUTS["f7_substation_saturation"].values()
    assert not [i for i in inputs if i.equivalence_class.startswith("capacity.free_mw_limited")]
    assert not [e for i in inputs for e in i.entries if "limited" in e]


def test_zone_and_municipality_aggregates_cover_dso_stations(rows: pl.DataFrame) -> None:
    dk1 = stats(rows, "dk.zone.dk1")
    assert dk1["stations"] == 4.0  # a, b, c, d; TSO stations and the unlocated station are not counted
    assert dk1["share_stations_no_free_consumption"] == pytest.approx(2 / 3)  # a, b of a, b, d
    assert dk1["share_stations_no_free_production"] == pytest.approx(1 / 4)  # d
    assert (dk1["free_mw_consumption_sum"], dk1["free_mw_production_sum"]) == (30.0, 140.0)
    dk2 = stats(rows, "dk.zone.dk2")
    assert dk2["stations"] == 1.0 and dk2["share_stations_no_free_consumption"] == 0.0
    assert "share_stations_no_free_production" not in dk2 and "free_mw_production_sum" not in dk2
    my = stats(rows, "m.y")
    assert my["stations"] == 2.0 and my["share_stations_no_free_consumption"] == 0.0
    assert my["share_stations_no_free_production"] == 0.5


def test_rows_carry_period_key_and_combined_sensitivity(rows: pl.DataFrame) -> None:
    assert rows.columns == list(substation.OUT_SCHEMA)
    assert set(rows["feature_key"]) == {"f7_substation_saturation"}
    assert set(rows["period_start"]) == {PERIOD[0]} and set(rows["period_end"]) == {PERIOD[1]}
    sens = dict(rows.select("entity_id", "sensitivity").unique().iter_rows())
    assert sens["e"] == "licensed" and sens["dk.zone.dk2"] == "licensed" and sens["m.z"] == "licensed"
    assert sens["a"] == sens["dk.zone.dk1"] == "public"
    assert rows["value"].is_finite().all()


def test_statistic_names_are_the_contract(rows: pl.DataFrame) -> None:
    station = {"free_mw_consumption", "free_mw_production", "rank_in_zone_consumption", "rank_in_zone_production"}
    dso = {
        "stations",
        "share_stations_no_free_consumption",
        "share_stations_no_free_production",
        "free_mw_consumption_sum",
        "free_mw_production_sum",
        "stations_assessed_consumption",
        "stations_assessed_production",
        "free_mw_consumption_mean",
        "free_mw_production_mean",
        "stations_assessed_both",
        "share_stations_free_consumption_no_free_production",
        "share_stations_free_production_no_free_consumption",
        "free_mw_consumption_mean_no_free_production",
        "free_mw_production_mean_no_free_consumption",
    }
    area = dso | {f"{s}_incl_tso" for s in dso if not s.endswith("_sum")}
    assert set(rows.filter(pl.col("entity_id") == "a")["statistic"]) == station
    assert set(rows.filter(pl.col("entity_id") == "dk.zone.dk1")["statistic"]) == area
    assert area == set(substation.AREA_STATISTICS)
    doc = substation.__doc__ or ""
    card = (REPO_ROOT / "docs" / "methods" / "f7_substation_saturation.md").read_text(encoding="utf-8")
    for name in station | dso:
        assert name in doc, name
    for name in dso:  # the card names each statistic, or its pattern with <d> for the direction
        generic = re.sub("consumption|production", "<d>", name, count=1)
        assert f"`{name}`" in card or f"`{generic}`" in card, name


def test_inputs_and_roles_agree() -> None:
    spec = FEATURES["f7_substation_saturation"]
    inputs = FEATURE_INPUTS["f7_substation_saturation"]
    assert set(inputs) == set(substation.ALL_ROLES)
    assert all(i.equivalence_class in spec.accepted_classes and len(i.entries) == 1 for i in inputs.values())
    with pytest.raises(ValueError, match="unknown F7 input roles"):
        substation.f7_rows(FACTS.with_columns(role=pl.lit("other")), PERIOD)
