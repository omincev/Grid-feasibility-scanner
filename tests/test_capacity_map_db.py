"""Capacity map, queue facts and F7 against a migrated database. Run with: TEST_DATABASE_URL=... pytest -m db

test_loader_* and test_queue_*: the curated reference data (zones, municipalities) and the loads run inside
one transaction that is rolled back; recorded fixtures stand in for the FeatureServer and Power BI.
test_f7_run_writes_traceable_rows commits a few test stations in a test zone, runs F7 through
run_features and removes what it wrote.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
import pytest

from scanner import REPO_ROOT
from scanner.adapters.energinet import connection_queue as cq
from scanner.adapters.kapacitetskort import stations as st
from scanner.core.curated import load_units, read_curated
from scanner.core.raw_store import RawStore
from scanner.core.registry import load_snapshots
from scanner.features import substation
from scanner.features.inputs import load_facts
from scanner.features.registry import FEATURE_INPUTS, FEATURES
from scanner.features.run import run_features
from scanner.runtime import snapshot_parsers
from scanner.runtime.capacity_map import load_capacity_map

pytestmark = pytest.mark.db

KAP = Path(__file__).parent / "fixtures" / "kapacitetskort"
QUEUE = Path(__file__).parent / "fixtures" / "energinet" / "connection_queue__capacity_by_region_phase.json"
SEP1 = datetime(2026, 9, 1, tzinfo=UTC)
DSO_ENTRY = f"dk.kapacitetskort.{st.DSO_LAYER}"
NAK = "dk.substation.dso.lolland.nak_nakskov"
DSO_FIXTURES = (
    "DSO_stations_capacity_points__lolland_bbox",
    "DSO_stations_capacity_points__ringkobing_skjern_bbox",
    "DSO_stations_capacity_points__stale_and_colliding",
)


def _collection(*names: str, drop: str | None = None) -> bytes:
    """One response assembled from recorded layer responses (feature records unchanged)."""
    features = [f for n in names for f in json.loads((KAP / f"{n}.json").read_bytes())["features"]]
    features = [f for f in features if drop is None or f["properties"].get("DSO_station_abbreviation") != drop]
    crs = {"type": "name", "properties": {"name": "EPSG:4326"}}
    return json.dumps({"type": "FeatureCollection", "crs": crs, "features": features}).encode()


def _fetch(drop: str | None = None) -> Any:
    responses = {
        st.SERVICE_URL: (KAP / "FeatureServer__service_index.json").read_bytes(),
        st.BOUNDARY_SERVICE_URL: (KAP / "Netselskabsgraenser__service_index.json").read_bytes(),
        f"{st.BOUNDARY_SERVICE_URL}/0/query": (KAP / "Netselskabsgraenser__geojson_coarse.json").read_bytes(),
        f"{st.SERVICE_URL}/1/query": _collection(*DSO_FIXTURES, drop=drop),
        f"{st.SERVICE_URL}/0/query": _collection("TSO_stations_capacity_points__four_stations"),
        f"{st.SERVICE_URL}/2/query": _collection("TSO_stations_fields_points__five_stations"),
    }

    def fetch(url: str, params: Mapping[str, str]) -> tuple[int, str, bytes]:
        return 200, "application/json", responses[url]

    return fetch


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection[Any]]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    c = psycopg.connect(url)
    c.execute("select 1")  # open the transaction: the loaders' blocks become savepoints, rolled back below
    load_units(c, read_curated(REPO_ROOT), restricted=False, dry_run=False)
    yield c
    c.rollback()
    c.close()


def _one(conn: psycopg.Connection[Any], sql: str, *args: Any) -> Any:
    row = conn.execute(sql, args).fetchone()
    assert row is not None
    return row[0] if len(row) == 1 else row


def _without_stored_facts(conn: psycopg.Connection[Any], prefix: str) -> None:
    """Facts an earlier load left in this database (the real capacity map, say) would make the loads below
    write nothing or win as later publications; the deletion is rolled back with the test's transaction."""
    conn.execute("delete from core.fact where attribute like %s", (f"{prefix}%",))


def test_loader_writes_entities_points_and_facts_once(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    _without_stored_facts(conn, "dk.kapacitetskort.")
    store = RawStore(tmp_path.as_uri())
    first = load_capacity_map(conn, fetch=_fetch(), store=store)
    assert first.status == "ok" and first.dso_areas == 40
    assert first.rows_per_layer == {st.DSO_LAYER: 78, st.TSO_LAYER: 7, st.BAY_LAYER: 5}
    assert first.entities_per_layer == {st.DSO_LAYER: 73, st.TSO_LAYER: 4, st.BAY_LAYER: 5}
    assert first.facts_written > 0 and not first.no_zone and not first.no_municipality
    assert any(q.entity_id == NAK for q in first.quirks)

    nak = _one(conn, "select parent_id, name, source_path, attributes from ref.entity where entity_id = %s", NAK)
    assert nak[:3] == ("dk.zone.dk2", "Nakskov", "runtime:kapacitetskort")
    assert nak[3]["municipality"] == "dk.municipality.lolland" and nak[3]["source_station_id"] == "NAK"
    assert nak[3]["listed"] is True
    # Listed: entities of this source that the run found (a real load may have left others, now unlisted).
    listed = "coalesce((attributes->>'listed')::boolean, true)"
    hasle = conn.execute(
        f"select entity_id, parent_id from ref.entity where entity_id like '%%.has_hasle' and {listed} order by 1"
    )
    assert hasle.fetchall() == [
        ("dk.substation.dso.aarhus.has_hasle", "dk.zone.dk1"),
        ("dk.substation.dso.bornholm.has_hasle", "dk.zone.dk2"),
    ]
    geom = _one(conn, "select source, st_geometrytype(geom) from ref.entity_geometry where entity_id = %s", NAK)
    assert geom == (f"kapacitetskort:{st.DSO_LAYER}", "ST_Point")
    areas = (
        "select count(*) from ref.entity where kind = 'dso_area' and source_path = 'runtime:kapacitetskort' "
        f"and {listed}"
    )
    assert _one(conn, areas) == 40

    # Klarup: the stale 2025-04-01 row (consumption not provided) and the 2026-09-01 row (0 MW)
    klarup = conn.execute(
        "select valid_from, value_num, value_json from core.fact where entity_id = %s and attribute = %s "
        "order by valid_from",
        ("dk.substation.dso.aalborg.kla_klarup", f"{DSO_ENTRY}.cap_cons_full_no_tso"),
    ).fetchall()
    assert [(v, n) for v, n, _ in klarup] == [(datetime(2025, 4, 1, tzinfo=UTC), None), (SEP1, 0.0)]
    run = uuid.UUID(first.run_id)
    assert _one(conn, "select count(*) from core.fact where value_json = 'null'::jsonb and run_id = %s", run) > 0
    cited = (
        "select count(*) from core.fact f join core.source_document d on d.document_id = f.source_document_id "
        "where f.run_id = %s and d.raw_uri like 'raw://kapacitetskort/%%'"
    )
    assert _one(conn, cited, run) == first.facts_written

    second = load_capacity_map(conn, fetch=_fetch(), store=store)
    assert second.status == "ok" and second.facts_written == 0
    record = "select status, rows_received, rows_written, cardinality(raw_uris) from ops.ingest_run where run_id = %s"
    assert _one(conn, record, run) == ("ok", 90, first.facts_written, 6)
    assert _one(conn, record, uuid.UUID(second.run_id)) == ("ok", 90, 0, 6)

    third = load_capacity_map(conn, fetch=_fetch(drop="VAR"), store=store)
    assert third.unlisted == 1 and third.facts_written == 0
    listed = "select attributes->>'listed' from ref.entity where entity_id = 'dk.substation.dso.varde.var_varde'"
    assert _one(conn, listed) == "false"


def test_a_layer_without_features_fails_the_run_and_unlists_nothing(
    conn: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """ADR 0013: zero rows where rows are expected is a hard deviation. An empty layer response (a broken
    layer or service) must not mark every station of the layer as no longer listed."""
    store = RawStore(tmp_path.as_uri())
    first = load_capacity_map(conn, fetch=_fetch(), store=store)
    full, empty = _fetch(), _collection()

    def fetch(url: str, params: Mapping[str, str]) -> tuple[int, str, bytes]:
        return (200, "application/json", empty) if url == f"{st.SERVICE_URL}/1/query" else full(url, params)

    with pytest.raises(st.ParseError, match="zero features"):
        load_capacity_map(conn, fetch=fetch, store=store)
    listed = (
        "select count(*) from ref.entity where source_path = 'runtime:kapacitetskort' "
        "and entity_id like 'dk.substation.dso.%%' and (attributes->>'listed')::boolean"
    )
    assert _one(conn, listed) == 73
    # runs of this test's transaction share its now()
    runs = "select status from ops.ingest_run where source = 'kapacitetskort' and started_at = now() and run_id <> %s"
    assert conn.execute(runs, (uuid.UUID(first.run_id),)).fetchall() == [("failed",)]


def test_loader_feeds_f7_and_the_serving_view(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    _without_stored_facts(conn, "dk.kapacitetskort.")
    load_capacity_map(conn, fetch=_fetch(), store=RawStore(tmp_path.as_uri()))
    spec = FEATURES["f7_substation_saturation"]
    facts = pl.concat(
        [
            load_facts(conn, spec.accepted_classes, inp, "dk").with_columns(role=pl.lit(role))
            for role, inp in FEATURE_INPUTS[spec.key].items()
        ]
    )
    rows = substation.f7_rows(facts, (SEP1, datetime(2026, 10, 1, tzinfo=UTC)))
    got = {(e, s): v for e, s, v in rows.select("entity_id", "statistic", "value").iter_rows()}
    assert got[(NAK, "free_mw_production")] == 40.0 and got[(NAK, "free_mw_consumption")] == 40.0
    # Varde: the 2026-09-01 values (production 20 and 20), not the stale 2026-01-01 row (60 and 60)
    assert got[("dk.substation.dso.varde.var_varde", "free_mw_production")] == 20.0
    assert got[("dk.substation.energinet.mal_150", "free_mw_production")] == 42.0  # existing grid
    assert ("dk.substation.energinet.bra_150_s", "free_mw_production") not in got  # reinforced grid only
    # DK2: the 20 Lolland-bbox stations and Mosedegaard, Saeby (Zealand), Haslev, Dalmose, Dalslunde, Hasle (Bornholm)
    assert got[("dk.zone.dk2", "stations")] == 26.0

    view = conn.execute(
        "select zone_or_area_id, capacity, attributes from serving.substation_map where entity_id = %s", (NAK,)
    ).fetchone()
    assert view is not None and view[0] == "dk.zone.dk2"
    solar = view[1][f"{DSO_ENTRY}.cap_solar_limited_lb"]
    assert solar["num"] == 55 and solar["valid_from"].startswith("2026-09-01")
    assert view[2]["source_station_id"] == "NAK"


def _queue_capture(store: RawStore) -> snapshot_parsers.Capture:
    """The recorded querydata response, with its second measure renamed to the last-updated measure the
    registry query asks for (same DSR shape: one dictionary value on every row)."""
    response = copy.deepcopy(json.loads(QUEUE.read_bytes()))
    data = response["results"][0]["result"]["data"]
    data["descriptor"]["Select"][4]["Name"] = cq.UPDATED
    data["dsr"]["DS"][0]["ValueDicts"]["D3"] = ["Senest opdateret: 01-10-2026"]
    content = json.dumps(response).encode()
    snap = {s.id: s for s in load_snapshots(REPO_ROOT)}["dk.energinet.connection_queue"]
    run_id, now = uuid.uuid4(), datetime.now(UTC)
    ref = store.put(
        source="snapshots",
        dataset=snap.id,
        run_id=str(run_id),
        seq=0,
        content=content,
        ext="json",
        fetched_at=now,
        meta={},
    )
    return snapshot_parsers.Capture(snap, content, "text/plain", ref, now, run_id)


def test_queue_parser_writes_region_facts_once(conn: psycopg.Connection[Any], tmp_path: Path) -> None:
    _without_stored_facts(conn, "dk.energinet.connection_queue.")
    store = RawStore(tmp_path.as_uri())
    with conn.transaction():
        first = snapshot_parsers.connection_queue(conn, _queue_capture(store))
    with conn.transaction():
        second = snapshot_parsers.connection_queue(conn, _queue_capture(store))
    assert (first.facts_written, second.facts_written, first.notes) == (30, 0, [])
    regions = conn.execute("select entity_id, parent_id from ref.entity where kind = 'tso_area' order by 1").fetchall()
    assert ("dk.tso_area.region_sjaelland", "dk.zone.dk2") in regions and len(regions) >= 5
    hovedstaden = conn.execute(
        "select attribute, value_num, valid_from from core.fact where entity_id = 'dk.tso_area.region_hovedstaden' "
        "and attribute like '%%consumption_screening'"
    ).fetchall()
    entry = "dk.energinet.connection_queue.Kapacitet_i_alt_MW.consumption_screening"
    assert hovedstaden == [(entry, 1964.0, datetime(2026, 10, 1, tzinfo=UTC))]


F7_DSO = [inp for role, inp in FEATURE_INPUTS["f7_substation_saturation"].items() if role.startswith("dso")]
ZONE, MUN, DOC = "dk.zone.test_f7", "dk.municipality.test_f7", "test:f7"


def test_f7_run_writes_traceable_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setenv("GIT_SHA", "testsha")
    stations = {"dk.substation.test_f7.s1": ("public", 0.0), "dk.substation.test_f7.s2": ("licensed", 40.0)}
    run_ids: list[uuid.UUID] = []
    with psycopg.connect(url) as c:
        _cleanup(c, run_ids)
        _f7_reference(c, stations)
        try:
            (outcome,) = run_features(["f7_substation_saturation"], database_url=url)
            run_ids.append(uuid.UUID(outcome.run_id))
            assert outcome.status == "ok" and outcome.rows_written > 0
            rows = c.execute(
                "select entity_id, statistic, value, sensitivity::text, run_id from feat.feature_value "
                "where run_id = %s and (entity_id like 'dk.substation.test_f7.%%' or entity_id in (%s, %s))",
                (run_ids[0], ZONE, MUN),
            ).fetchall()
            got = {(e, s): (v, sens) for e, s, v, sens, _ in rows}
            assert got[("dk.substation.test_f7.s1", "free_mw_production")] == (0.0, "public")
            assert got[("dk.substation.test_f7.s2", "rank_in_zone_production")] == (2.0, "licensed")
            assert got[(ZONE, "share_stations_no_free_production")] == (0.5, "licensed")
            assert got[(MUN, "stations")] == (2.0, "licensed")
            run = c.execute(
                "select kind, name, status, code_version from ops.compute_run where run_id = %s", (run_ids[0],)
            ).fetchone()
            assert run == ("feature", "f7_substation_saturation", "ok", "testsha")
        finally:
            _cleanup(c, run_ids)


def _f7_reference(c: psycopg.Connection[Any], stations: dict[str, tuple[str, float]]) -> None:
    c.execute(
        "insert into ref.country (code, name, status, timezone, currency, config, source_path, content_hash) "
        "values ('dk','Denmark','active','Europe/Copenhagen','DKK','{}','t','t') on conflict do nothing"
    )
    for entity, kind in ((ZONE, "zone"), (MUN, "municipality")):
        c.execute(
            "insert into ref.entity (entity_id, kind, country_code, name) values (%s, %s, 'dk', 't')", (entity, kind)
        )
    for inp in F7_DSO:
        c.execute(
            "insert into ref.equivalence_class values (%s, 'power', 'x') on conflict do nothing",
            (inp.equivalence_class,),
        )
        c.execute(
            "insert into ref.codebook_entry (entry_id, source, dataset, country_code, field, equivalence_class, "
            "kind, unit, resample, definition, status, source_path, content_hash) values (%s, 'kapacitetskort', "
            "'DSO_stations_capacity_points', 'dk', 'x', %s, 'capacity', 'MW', 'none', '{}', 'draft', 't', 't') "
            "on conflict do nothing",
            (inp.entries[0], inp.equivalence_class),
        )
    c.execute(
        "insert into core.source_document (document_id, source, fetched_at, sha256, raw_uri, content_type) "
        "values (%s, 'test', now(), 'x', 'file://x', 'application/json')",
        (DOC,),
    )
    for eid, (sens, mw) in stations.items():
        c.execute(
            "insert into ref.entity (entity_id, kind, country_code, parent_id, name, attributes) "
            "values (%s, 'substation', 'dk', %s, 't', %s)",
            (eid, ZONE, json.dumps({"municipality": MUN, "listed": True})),
        )
        for inp in F7_DSO:
            c.execute(
                "insert into core.fact (entity_id, attribute, value_num, unit, valid_from, known_at, "
                "source_document_id, sensitivity) values (%s, %s, %s, 'MW', %s, now(), %s, %s)",
                (eid, inp.entries[0], mw, SEP1, DOC, sens),
            )
    c.commit()


def _cleanup(c: psycopg.Connection[Any], run_ids: list[uuid.UUID]) -> None:
    c.execute("delete from feat.feature_value where run_id = any(%s)", (run_ids,))
    c.execute("delete from ops.compute_run where run_id = any(%s)", (run_ids,))
    c.execute("delete from core.fact where entity_id like 'dk.substation.test_f7.%'")
    c.execute("delete from core.source_document where document_id = %s", (DOC,))
    c.execute("delete from ref.entity where entity_id like 'dk.substation.test_f7.%'")
    c.execute("delete from ref.entity where entity_id in (%s, %s)", (ZONE, MUN))
    c.commit()
