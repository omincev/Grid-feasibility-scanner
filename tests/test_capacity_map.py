"""Capacity-map parsing (WP1.5) on recorded fixtures: slugs and entity IDs, null versus 0, stale rows as
earlier versions, lower-above-upper quirks, paging, and the append-only fact selection."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scanner.adapters.kapacitetskort import stations as st
from scanner.core.db import FactRow, StoredFact, select_new_facts
from scanner.core.raw_store import RawStore
from scanner.runtime import capacity_map

FIX = Path(__file__).parent / "fixtures" / "kapacitetskort"
SEP1, JAN1, APR1_25 = (datetime(y, m, 1, tzinfo=UTC) for y, m in ((2026, 9), (2026, 1), (2025, 4)))


def fixture(name: str) -> Any:
    return json.loads((FIX / f"{name}.json").read_bytes())


def rows(layer: st.Layer, name: str) -> list[st.StationRow]:
    return st.parse_layer(layer, fixture(name), f"doc:{name}")


def by_longitude(points: list[tuple[float, float]]) -> dict[tuple[float, float], st.Location]:
    """A stand-in for the spatial join: Bornholm east of 14 deg, Zealand east of 11, Jutland west of it."""

    def loc(lon: float) -> st.Location:
        if lon > 14:
            return st.Location(("dk.zone.dk2",), ("dk.municipality.bornholm",), ("dk.dso_area.oest",))
        if lon > 11:
            return st.Location(("dk.zone.dk2",), ("dk.municipality.kalundborg",), ("dk.dso_area.cerius",))
        return st.Location(("dk.zone.dk1",), ("dk.municipality.aarhus",), ("dk.dso_area.n1", "dk.dso_area.rah"))

    return {p: loc(p[0]) for p in points}


def build(*parsed: list[st.StationRow]) -> st.Stations:
    every = [r for rs in parsed for r in rs]
    return st.build_stations(every, by_longitude([r.point for r in every]))


def values(s: st.Stations, entity: str, entry_suffix: str) -> list[tuple[datetime | None, Any]]:
    return sorted(
        ((c.valid_from, c.value) for c in s.candidates if c.entity_id == entity and c.entry_id.endswith(entry_suffix)),
        key=lambda x: x[0] or datetime.min.replace(tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Mosedegård", "mosedegaard"),
        ("Rødbyhavn", "roedbyhavn"),
        ("ÅBØ_150", "aaboe_150"),
        ("H. C. Ørsted Værket", "h_c_oersted_vaerket"),
        ("Nr. alslev", "nr_alslev"),
        ("BRA_150--$", "bra_150_s"),
        ("BRA_150", "bra_150"),
        ("Søndervig ", "soendervig"),
    ],
)
def test_slug(text: str, expected: str) -> None:
    assert st.slug(text) == expected


def test_slug_rejects_empty() -> None:
    with pytest.raises(ValueError, match="cannot build a slug"):
        st.slug(" -- ")


def test_publication_time() -> None:
    assert st.publication_time(1788220800000) == SEP1
    assert st.publication_time("2026-04-01") == datetime(2026, 4, 1, tzinfo=UTC)
    assert st.publication_time(None) is None
    with pytest.raises(st.ParseError):
        st.publication_time("N.A")


def test_parse_layer_reads_points_codes_and_dates() -> None:
    parsed = rows(st.DSO, "DSO_stations_capacity_points__sample")
    assert [(r.code, r.name) for r in parsed][:2] == [("FBY", "Faaborg by"), ("LUA", "Lundager")]
    assert parsed[0].point == (10.2489160000002, 55.0956949991126)
    assert parsed[0].published == SEP1 and parsed[0].document_id == "doc:DSO_stations_capacity_points__sample"


def test_parse_layer_rejects_a_missing_field_and_a_non_point() -> None:
    fc = fixture("DSO_stations_capacity_points__sample")
    broken = copy.deepcopy(fc)
    del broken["features"][0]["properties"]["available_capacity_wind_MW_upper_bound"]
    with pytest.raises(st.ParseError, match="fields missing"):
        st.parse_layer(st.DSO, broken, "d")
    broken = copy.deepcopy(fc)
    broken["features"][0]["geometry"] = {"type": "Polygon", "coordinates": []}
    with pytest.raises(st.ParseError, match="not a point"):
        st.parse_layer(st.DSO, broken, "d")
    broken = copy.deepcopy(fc)
    broken["crs"] = {"type": "name", "properties": {"name": "EPSG:25832"}}
    with pytest.raises(st.ParseError, match="outSR=4326"):
        st.parse_layer(st.DSO, broken, "d")


def test_dso_entity_ids_separate_same_named_stations_and_merge_stale_rows() -> None:
    parsed = rows(st.DSO, "DSO_stations_capacity_points__stale_and_colliding")
    s = build(parsed)
    assert len(parsed) == 18 and s.entities_per_layer == {st.DSO_LAYER: 13}  # 5 stale rows merged
    ids = set(s.entities)
    # "HAS Hasle" in Aarhus and on Bornholm, "SBY Saeby" in Jutland and on Zealand: different stations
    assert {"dk.substation.dso.aarhus.has_hasle", "dk.substation.dso.bornholm.has_hasle"} <= ids
    assert {"dk.substation.dso.aarhus.sby_saeby", "dk.substation.dso.kalundborg.sby_saeby"} <= ids
    klarup = s.entities["dk.substation.dso.aarhus.kla_klarup"]
    assert klarup.name == "Klarup" and klarup.parent_id == "dk.zone.dk1"
    assert klarup.attributes["tso_station_abbreviation"] == "FER"  # the 2026-09-01 row, not the stale one
    assert klarup.attributes["source_station_id"] == "KLA" and klarup.attributes["layers"] == [st.DSO_LAYER]
    assert s.id_collisions == [] and s.conflicts == []


def test_null_stays_null_and_zero_stays_zero() -> None:
    s = build(rows(st.DSO, "DSO_stations_capacity_points__stale_and_colliding"))
    # Klarup: consumption not provided in the 2025-04-01 row, published 0 in the 2026-09-01 row
    assert values(s, "dk.substation.dso.aarhus.kla_klarup", "cap_cons_full_no_tso") == [(APR1_25, None), (SEP1, 0.0)]
    ikast = values(s, "dk.substation.dso.aarhus.ika_ikast", "cap_cons_full_no_tso")
    assert ikast == [(APR1_25, None), (JAN1, 10.0), (SEP1, 10.0)]


def test_ambiguous_dso_is_null_with_candidates() -> None:
    s = build(rows(st.DSO, "DSO_stations_capacity_points__stale_and_colliding"))
    varde = s.entities["dk.substation.dso.aarhus.var_varde"].attributes
    assert varde["dso"] is None and varde["dso_candidates"] == ["dk.dso_area.n1", "dk.dso_area.rah"]
    assert s.entities["dk.substation.dso.bornholm.has_hasle"].attributes["dso"] == "dk.dso_area.oest"


def test_lower_bound_above_upper_bound_is_kept_and_reported() -> None:
    s = build(rows(st.DSO, "DSO_stations_capacity_points__lolland_bbox"))
    nak = "dk.substation.dso.kalundborg.nak_nakskov"
    quirks = {(q.lower_field, q.lower, q.upper) for q in s.quirks if q.entity_id == nak}
    assert quirks == {
        ("available_capacity_solar_MW_lower_bound", 55.0, 0.0),
        ("available_capacity_wind_MW_lower_bound", 45.0, 0.0),
    }
    assert values(s, nak, "cap_solar_limited_lb") == [(SEP1, 55.0)]
    assert values(s, nak, "cap_solar_limited_ub") == [(SEP1, 0.0)]


def test_unlocated_station_gets_the_country_as_parent() -> None:
    parsed = rows(st.DSO, "DSO_stations_capacity_points__sample")
    s = st.build_stations(parsed, {r.point: st.Location() for r in parsed})
    e = s.entities["dk.substation.dso.unlocated.fby_faaborg_by"]
    assert e.parent_id == "dk.country.dk" and e.attributes["zone"] is None and e.attributes["municipality"] is None
    assert e.attributes["dso"] is None and "dso_candidates" not in e.attributes


def test_tso_layers_share_entities_by_exact_code_only() -> None:
    s = build(
        rows(st.TSO, "TSO_stations_capacity_points__four_stations"),
        rows(st.BAY, "TSO_stations_fields_points__five_stations"),
    )
    assert s.rows_per_layer == {st.TSO_LAYER: 7, st.BAY_LAYER: 5}
    mal = s.entities["dk.substation.energinet.mal_150"]
    assert mal.attributes["layers"] == [st.TSO_LAYER, st.BAY_LAYER]
    assert mal.attributes["voltage_kv"] == 150 and mal.attributes["station_status"] == "Aktuel"
    assert mal.attributes["grid_preconditions"] == ["existing_grid", "reinforced_grid"]
    assert mal.geometry_source == f"kapacitetskort:{st.TSO_LAYER}"
    assert values(s, mal.entity_id, "available_capacity_production_MW.existing_grid") == [
        (datetime(2026, 2, 1, tzinfo=UTC), 42.0)
    ]
    assert values(s, mal.entity_id, "available_capacity_production_MW.reinforced_grid") == [
        (datetime(2026, 2, 1, tzinfo=UTC), 385.0)
    ]
    assert values(s, mal.entity_id, "available_fields_for_publication_capacitymap") == [
        (datetime(2026, 4, 1, tzinfo=UTC), "Ikke afdækket")
    ]
    # BRA_150--$ ("Hedensted") and BRA_150 ("Bredal", 2.5 km away) are not merged
    assert s.entities["dk.substation.energinet.bra_150_s"].name == "Hedensted"
    assert s.entities["dk.substation.energinet.bra_150"].name == "Bredal"
    assert "dk.substation.energinet.mal_400" in s.entities


def test_same_date_different_values_is_a_conflict() -> None:
    fc = fixture("DSO_stations_capacity_points__sample")
    twin = copy.deepcopy(fc["features"][0])
    twin["properties"]["available_capacity_production_MW_DSO_station_with_TSO_transforme"] = 40
    fc["features"].append(twin)
    parsed = st.parse_layer(st.DSO, fc, "d")
    s = st.build_stations(parsed, by_longitude([r.point for r in parsed]))
    fby = "dk.substation.dso.aarhus.fby_faaborg_by"
    assert values(s, fby, "cap_prod_full_with_tso") == []
    assert len(s.conflicts) == 1 and "cap_prod_full_with_tso" in s.conflicts[0]
    assert values(s, fby, "cap_prod_full_no_tso") == [(SEP1, 0.0)]  # equal values are not a conflict


def test_dso_areas() -> None:
    areas = st.parse_dso_areas(fixture("Netselskabsgraenser__geojson_coarse"))
    ids = {a.entity_id for a in areas}
    assert len(areas) == 40 == len(ids)
    assert {"dk.dso_area.n1", "dk.dso_area.cerius", "dk.dso_area.oest", "dk.dso_area.el_net_randers"} <= ids
    n1 = next(a for a in areas if a.entity_id == "dk.dso_area.n1")
    assert n1.name == "N1 A/S" and n1.attributes["selsk_nr"] == 3159
    assert n1.geometry["type"] in ("Polygon", "MultiPolygon")


def test_layer_ids_by_name() -> None:
    ids = capacity_map.layer_ids(fixture("FeatureServer__service_index"))
    assert ids[st.DSO_LAYER] == 1 and ids[st.TSO_LAYER] == 0 and ids[st.BAY_LAYER] == 2
    assert capacity_map.layer_ids(fixture("Netselskabsgraenser__service_index")) == {"Netselskabsgraenser": 0}


def test_layer_pages_past_the_transfer_limit(tmp_path: Path) -> None:
    sample = fixture("DSO_stations_capacity_points__sample")  # 3 features, exceededTransferLimit true
    last = {**fixture("DSO_stations_capacity_points__lolland_bbox")}
    calls: list[Mapping[str, str]] = []

    def fetch(url: str, params: Mapping[str, str]) -> tuple[int, str, bytes]:
        calls.append(params)
        body = sample if params["resultOffset"] == "0" else last
        return 200, "application/json", json.dumps(body).encode()

    cap = capacity_map._Captures(fetch, RawStore(tmp_path.as_uri()), uuid.uuid4())
    index = {"maxRecordCount": 3, "layers": [{"id": 1, "name": st.DSO_LAYER}]}
    pages = cap.layer(st.SERVICE_URL, index, st.DSO_LAYER)
    assert [p["resultOffset"] for p in calls] == ["0", "3"] and len(pages) == 2
    assert all(p["outSR"] == "4326" and p["f"] == "geojson" and p["resultRecordCount"] == "3" for p in calls)
    assert len(cap.documents) == 2
    assert all(d["raw_uri"].startswith(f"raw://kapacitetskort/{st.DSO_LAYER}/") for d in cap.documents)
    with pytest.raises(st.ParseError, match="not in"):
        cap.layer(st.SERVICE_URL, index, "Renamed_layer")


def test_service_error_in_a_200_response_fails(tmp_path: Path) -> None:
    def fetch(url: str, params: Mapping[str, str]) -> tuple[int, str, bytes]:
        return 200, "application/json", b'{"error": {"code": 400, "message": "Invalid query"}}'

    cap = capacity_map._Captures(fetch, RawStore(tmp_path.as_uri()), uuid.uuid4())
    with pytest.raises(RuntimeError, match="service error"):
        cap.json(st.SERVICE_URL, {"f": "json"}, "service_index")


def fact(value: float | str | None, valid_from: datetime | None, attribute: str = "a") -> FactRow:
    return FactRow("e", attribute, value, "MW", valid_from, datetime(2026, 10, 18, tzinfo=UTC), "doc", None)


def stored(rows: list[FactRow]) -> list[StoredFact]:
    return [StoredFact(r.entity_id, r.attribute, r.valid_from, r.known_at, i, r.value) for i, r in enumerate(rows)]


def test_select_new_facts_is_append_only() -> None:
    first = [fact(None, APR1_25), fact(10.0, JAN1), fact(10.0, SEP1), fact(0.0, SEP1, "b")]
    new = select_new_facts(first, [])
    assert [(f.attribute, f.valid_from, f.value) for f in new] == [
        ("a", APR1_25, None),
        ("a", JAN1, 10.0),  # null -> 10 is a change
        ("b", SEP1, 0.0),
    ]  # 10 at SEP1 repeats 10 from JAN1: not written
    assert select_new_facts(first, stored(new)) == []  # a second run writes nothing
    assert select_new_facts([fact(10.0, datetime(2026, 12, 1, tzinfo=UTC))], stored(new)) == []  # republished
    changed = select_new_facts([fact(20.0, datetime(2026, 12, 1, tzinfo=UTC))], stored(new))
    assert [f.value for f in changed] == [20.0]
    corrected = select_new_facts([fact(0.0, JAN1)], stored(new))  # in-place correction, same valid_from
    assert [f.value for f in corrected] == [0.0]
    assert [f.value for f in select_new_facts([fact(None, SEP1, "b")], stored(new))] == [None]  # 0 -> null
    assert [f.value for f in select_new_facts([fact(0.0, APR1_25)], stored(new))] == [0.0]  # null -> 0


def test_cli_command_and_dagster_wiring() -> None:
    from dagster import AssetKey

    from scanner import cli
    from scanner.analytics_definitions import defs as analytics
    from scanner.ingestion_definitions import defs

    assert cli.build_parser().parse_args(["capacity-map"]).handler is cli.capacity_map_cmd
    graph = defs.resolve_asset_graph()
    capacity = AssetKey(["capacity", "kapacitetskort"])
    assert capacity in graph.get_all_asset_keys() and graph.get(capacity).is_materializable
    f7 = analytics.resolve_asset_graph().get(AssetKey(["features", "f7_substation_saturation"]))
    assert capacity in f7.parent_keys
    schedules = {s.name: s for s in defs.schedules or []}
    assert schedules["schedule_capacity_kapacitetskort"].cron_schedule == "30 6 * * 1"
