"""FCR capacity markets (FcrNdDK2, FcrDK1) parsed from real recorded responses.

Dossier: docs/sources/dk/eds-reserves-fcr.md. Fixtures were recorded with scripts/record_fixture.py; the
sidecar of each has the exact URL. Every assertion is about what the source actually published.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import FIXTURES

from scanner.adapters.base import FetchResult, ParsedBatch, ParseError
from scanner.adapters.energidataservice import EnergiDataServiceAdapter
from scanner.adapters.energidataservice.adapter import series_key
from scanner.core.manifest import Manifest
from scanner.core.time import expected_intervals, local_day_bounds, parse_source_utc
from scanner.runtime.context import RepoState

EDS = FIXTURES / "energidataservice"
TZ = "Europe/Copenhagen"

# (dataset, case, local day, hours that day)
DAY_CASES = [
    ("FcrNdDK2", "normal_day", date(2026, 10, 14), 24),
    ("FcrNdDK2", "dst_start", date(2026, 3, 29), 23),
    ("FcrDK1", "normal_day", date(2026, 10, 14), 24),
    ("FcrDK1", "dst_end", date(2025, 10, 26), 25),
    ("FcrDK1", "dst_start", date(2026, 3, 29), 23),
    ("FFRDK2", "normal_day", date(2026, 10, 14), 24),
]
# What the source published differs from the calendar on DST days (dossier, "Datasets"): FcrDK1 lacks
# 2025-10-26T01:00Z and FcrNdDK2 lacks 2026-03-29T21:00Z. Both are stored as published, nothing filled.
SOURCE_GAPS: dict[tuple[str, str], set[datetime]] = {
    ("FcrNdDK2", "dst_start"): {datetime(2026, 3, 29, 21, tzinfo=UTC)},
    ("FcrDK1", "dst_end"): {datetime(2025, 10, 26, 1, tzinfo=UTC)},
}
PRODUCTS = {"FCR-N": "fcr_n", "FCR-D upp": "fcr_d_up", "FCR-D ned": "fcr_d_down"}
AUCTIONS = {"D-1 early": "d1_early", "D-1 late": "d1_late", "Total": "total"}


def _result(dataset: str, case: str) -> FetchResult:
    meta = json.loads((EDS / f"{dataset}__{case}.json.meta.json").read_text())
    return FetchResult(
        url=meta["url"],
        params={},
        status=meta["status"],
        content_type=meta["content_type"],
        content=(EDS / f"{dataset}__{case}.json").read_bytes(),
        fetched_at=datetime.fromisoformat(meta["fetched_at"]),
    )


def _raw(dataset: str, case: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = json.loads((EDS / f"{dataset}__{case}.json").read_bytes())["records"]
    return records


def _manifest(state: RepoState, dataset: str) -> Manifest:
    return next(m for m in state.manifests if m.dataset == dataset)


def _parse(state: RepoState, dataset: str, case: str, day: date) -> ParsedBatch:
    m = _manifest(state, dataset)
    adapter = EnergiDataServiceAdapter(
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(599)))
    )
    start, end = local_day_bounds(day, TZ)
    return adapter.parse(m, state.countries["dk"], state.entries_for(m), _result(dataset, case), start, end)


def test_manifests_declare_the_dataset_structure(state: RepoState) -> None:
    nd, dk1 = _manifest(state, "FcrNdDK2"), _manifest(state, "FcrDK1")
    assert (nd.zone_field, nd.zones, nd.records_per_interval_per_zone) == ("PriceArea", {"DK2": "dk.zone.dk2"}, 9)
    assert (dk1.zone_field, dk1.zones, dk1.records_per_interval_per_zone) == (None, {"DK1": "dk.zone.dk1"}, 1)
    for m in (nd, dk1):
        assert (m.status, m.resolution, m.time_field_utc, m.sensitivity) == ("active", "PT1H", "HourUTC", "public")
    assert len(nd.entries) == 27 and len(dk1.entries) == 4


@pytest.mark.parametrize(("dataset", "case", "day", "n"), DAY_CASES, ids=[f"{d}-{c}" for d, c, _, _ in DAY_CASES])
def test_real_day_is_complete_utc_and_unfilled(state: RepoState, dataset: str, case: str, day: date, n: int) -> None:
    m = _manifest(state, dataset)
    entries = state.entries_for(m)
    start, end = local_day_bounds(day, TZ)
    batch = _parse(state, dataset, case, day)
    zone_id = next(iter(m.zones.values()))

    assert set(batch.series) == {series_key(m, zone_id, e) for e in entries}
    expected = [s for s, _ in expected_intervals(day, TZ, m.resolution_td)]
    assert len(expected) == n
    expected = sorted(set(expected) - SOURCE_GAPS.get((dataset, case), set()))
    for key in batch.series:
        assert sorted(r.interval_start for r in batch.rows if r.series_key == key) == expected, key
    assert max(Counter((r.series_key, r.interval_start, r.issued_at) for r in batch.rows).values()) == 1
    for row in batch.rows:
        assert row.interval_start.tzinfo is UTC
        assert row.interval_end - row.interval_start == timedelta(hours=1)

    # One row per source value, unchanged; nulls stay null.
    source: dict[tuple[str, datetime], Any] = {}
    for r in _raw(dataset, case):
        t0 = parse_source_utc(r["HourUTC"])
        if not start <= t0 < end or (m.zone_field and r[m.zone_field] not in m.zones):
            continue
        for e in entries:
            if all(r.get(k) == v for k, v in e.dimensions.items()):
                source[(series_key(m, zone_id, e), t0)] = r[e.field]
    assert len(source) == len(batch.rows)
    for row in batch.rows:
        value = source[(row.series_key, row.interval_start)]
        assert row.value == (None if value is None else float(value))


def test_nd_publishes_nine_dk2_records_per_hour_and_other_areas(state: RepoState) -> None:
    raw = _raw("FcrNdDK2", "normal_day")
    assert {r["PriceArea"] for r in raw} == {"DK2"}  # recorded with the manifest's DK2 filter
    per_hour = Counter(r["HourUTC"] for r in raw)
    assert set(per_hour.values()) == {9}
    assert {(r["ProductName"], r["AuctionType"]) for r in raw} == {(p, a) for p in PRODUCTS for a in AUCTIONS}


def test_nd_series_are_selected_by_product_and_auction(state: RepoState) -> None:
    batch = _parse(state, "FcrNdDK2", "normal_day", date(2026, 10, 14))
    for pname, pkey in PRODUCTS.items():
        for aname, akey in AUCTIONS.items():
            key = f"dk.eds.FcrNdDK2.dk2.PriceTotalEUR.{pkey}_{akey}"
            assert key in batch.series, key
            raw = {
                parse_source_utc(r["HourUTC"]): r["PriceTotalEUR"]
                for r in _raw("FcrNdDK2", "normal_day")
                if (r["ProductName"], r["AuctionType"]) == (pname, aname)
            }
            got = {r.interval_start: r.value for r in batch.rows if r.series_key == key}
            assert got and all(got[t] == raw[t] for t in got)


def test_nd_total_is_between_the_two_auction_prices(state: RepoState) -> None:
    """Since 2024-02-01 Total is the volume weighted average of the D-1 early and D-1 late marginal prices."""
    by_hour: dict[tuple[str, str], dict[str, float]] = {}
    for r in _raw("FcrNdDK2", "normal_day"):
        by_hour.setdefault((r["HourUTC"], r["ProductName"]), {})[r["AuctionType"]] = r["PriceTotalEUR"]
    for prices in by_hour.values():
        lo, hi = sorted((prices["D-1 early"], prices["D-1 late"]))
        assert lo - 1e-6 <= prices["Total"] <= hi + 1e-6


def test_dk1_has_one_zone_and_four_series(state: RepoState) -> None:
    batch = _parse(state, "FcrDK1", "normal_day", date(2026, 10, 14))
    assert {s.entity_id for s in batch.series.values()} == {"dk.zone.dk1"}
    assert {k.split(".")[-1] for k in batch.series} == {
        "FCRdomestic_MW",
        "FCRabroad_MW",
        "FCRcross_EUR",
        "FCRdk_EUR",
    }
    assert "PriceArea" not in _raw("FcrDK1", "normal_day")[0]


def test_nd_history_start_nulls_stay_null(state: RepoState) -> None:
    """The first recorded day (2021-11-10) holds explicit nulls in the measures; none is filled."""
    batch = _parse(state, "FcrNdDK2", "history_start", date(2021, 11, 10))
    nulls = [r for r in batch.rows if r.value is None]
    assert nulls
    raw_nulls = sum(
        rec[f] is None
        for rec in _raw("FcrNdDK2", "history_start")
        for f in ("PurchasedVolumeLocal", "PurchasedVolumeTotal", "PriceTotalEUR")
        if parse_source_utc(rec["HourUTC"]) >= local_day_bounds(date(2021, 11, 10), TZ)[0]
    )
    assert len(nulls) == raw_nulls


def test_nd_dst_end_mis_stamped_hours_are_a_parse_error(state: RepoState) -> None:
    """On 2025-10-26 the source has no 00:00Z record and stamps the last hour 01:01Z (HourDK 02:01) while
    01:00Z carries HourDK 02:00, the hour that really starts at 00:00Z. Storing it would give overlapping,
    wrong intervals, so the off-grid stamp fails the parse and the day becomes a hard deviation."""
    with pytest.raises(ParseError, match="not on the PT1H grid"):
        _parse(state, "FcrNdDK2", "dst_end", date(2025, 10, 26))


def test_ffr_dk2_has_no_zone_column_and_three_series(state: RepoState) -> None:
    m = _manifest(state, "FFRDK2")
    assert (m.zone_field, m.zones, m.resolution) == (None, {"DK2": "dk.zone.dk2"}, "PT1H")
    batch = _parse(state, "FFRDK2", "normal_day", date(2026, 10, 14))
    assert {k.split(".")[-1] for k in batch.series} == {"FFR_DemandMW", "FFR_PurchasedMW", "FFR_PriceEUR"}
    assert "PriceArea" not in _raw("FFRDK2", "normal_day")[0]
