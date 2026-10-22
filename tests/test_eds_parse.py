"""Energi Data Service parsing against real recorded responses (WP1.1, docs/quality.md layer 1).

Fixtures were recorded with scripts/record_fixture.py; each has a .meta.json sidecar with the URL and
fetch time. Every assertion is about what the source actually published.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from conftest import FIXTURES

from scanner.adapters.base import FetchResult, ParsedBatch, ParseError
from scanner.adapters.energidataservice import EnergiDataServiceAdapter
from scanner.adapters.energidataservice.adapter import series_key
from scanner.core.db import ObservationRow
from scanner.core.fingerprint import Baseline, compare, compute, severity
from scanner.core.manifest import Manifest
from scanner.core.time import expected_intervals, local_day_bounds, parse_source_utc
from scanner.runtime.context import RepoState
from scanner.runtime.ingest import (
    expected_partition_rows,
    ingest_windows,
    partition_records,
    quarantine_partition_day,
)

EDS = FIXTURES / "energidataservice"
TZ = "Europe/Copenhagen"

# (dataset, case, local day the fixture covers, intervals per series that day)
DAY_CASES = [
    ("DayAheadPrices", "normal_day", date(2026, 10, 14), 96),
    ("DayAheadPrices", "dst_end", date(2025, 10, 26), 100),
    ("DayAheadPrices", "dst_start", date(2026, 3, 29), 92),
    ("DayAheadPrices", "resolution_switch", date(2025, 10, 1), 96),
    ("Elspotprices", "normal_day", date(2025, 6, 10), 24),
    ("Elspotprices", "dst_start", date(2025, 3, 30), 23),
    ("Elspotprices", "dst_end", date(2024, 10, 27), 25),
    ("Elspotprices", "resolution_switch", date(2025, 9, 30), 24),
    ("ImbalancePrice", "normal_day", date(2026, 10, 14), 96),
    ("ImbalancePrice", "dst_end", date(2025, 10, 26), 100),
    ("ImbalancePrice", "dst_start", date(2026, 3, 29), 92),
    ("ImbalancePrice", "explicit_nulls", date(2025, 3, 14), 96),
    ("Forecasts_Hour", "normal_day", date(2026, 10, 14), 24),
    ("Forecasts_Hour", "dst_end", date(2025, 10, 26), 25),
    ("Forecasts_Hour", "dst_start", date(2026, 3, 29), 23),
    ("AfrrReservesNordic", "normal_day", date(2026, 10, 14), 24),
    ("AfrrReservesNordic", "dst_end", date(2025, 10, 26), 25),
    ("AfrrReservesNordic", "dst_start", date(2026, 3, 29), 23),
    ("AfrrReservesNordic", "history_start", date(2024, 10, 2), 24),
    ("AfrrReservesNordic", "demand_start", date(2025, 8, 21), 24),
    ("GenerationProdTypeExchange", "normal_day", date(2026, 10, 14), 24),
    ("GenerationProdTypeExchange", "dst_end", date(2025, 10, 26), 25),
    ("GenerationProdTypeExchange", "dst_start", date(2026, 3, 29), 23),
]
EMPTY_CASES = [
    ("DayAheadPrices", date(2025, 9, 30)),  # the day before the 15-minute dataset starts
    ("Elspotprices", date(2025, 10, 26)),  # after the hourly dataset stopped
    ("ImbalancePrice", date(2025, 3, 3)),  # the day before the dataset starts
    ("Forecasts_Hour", date(2024, 4, 14)),  # permanently missing at the source
    ("AfrrReservesNordic", date(2026, 10, 24)),  # after the last published day (tomorrow's results only)
    ("GenerationProdTypeExchange", date(2014, 12, 30)),  # before the dataset starts
]


def _result(dataset: str, case: str, fetched_at: datetime | None = None) -> FetchResult:
    meta = json.loads((EDS / f"{dataset}__{case}.json.meta.json").read_text())
    return FetchResult(
        url=meta["url"],
        params={},
        status=meta["status"],
        content_type=meta["content_type"],
        content=(EDS / f"{dataset}__{case}.json").read_bytes(),
        fetched_at=fetched_at or datetime.fromisoformat(meta["fetched_at"]),
    )


def _raw_records(dataset: str, case: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = json.loads((EDS / f"{dataset}__{case}.json").read_bytes())["records"]
    return records


def _adapter() -> EnergiDataServiceAdapter:
    return EnergiDataServiceAdapter(client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(599))))


def _manifest(state: RepoState, dataset: str) -> Manifest:
    return next(m for m in state.manifests if m.dataset == dataset)


def _parse(state: RepoState, dataset: str, result: FetchResult, start: datetime, end: datetime) -> ParsedBatch:
    m = _manifest(state, dataset)
    return _adapter().parse(m, state.countries["dk"], state.entries_for(m), result, start, end)


def _day(day: date) -> tuple[datetime, datetime]:
    return local_day_bounds(day, TZ)


@pytest.mark.parametrize(("dataset", "case", "day", "n"), DAY_CASES, ids=[f"{d}-{c}" for d, c, _, _ in DAY_CASES])
def test_real_day_is_complete_unique_utc_and_unfilled(
    state: RepoState, dataset: str, case: str, day: date, n: int
) -> None:
    m = _manifest(state, dataset)
    entries = state.entries_for(m)
    start, end = _day(day)
    batch = _parse(state, dataset, _result(dataset, case), start, end)
    assert m.zone_field is not None
    raw = [
        r
        for r in _raw_records(dataset, case)
        if start <= parse_source_utc(r[m.time_field_utc]) < end and r[m.zone_field] in m.zones
    ]

    # Every zone the source published and the manifest covers yields every entry's series.
    zones = {r[m.zone_field] for r in raw}
    assert zones >= {"DK1", "DK2"}
    assert set(batch.series) == {series_key(m, m.zones[z], e) for z in zones for e in entries}

    # Interval count per local day equals expected_intervals, with exactly those interval starts.
    expected = [s for s, _ in expected_intervals(day, TZ, m.resolution_td)]
    assert len(expected) == n
    starts: dict[str, list[datetime]] = defaultdict(list)
    for row in batch.rows:
        starts[row.series_key].append(row.interval_start)
    for key in batch.series:
        assert sorted(starts[key]) == expected, key

    # (series, interval_start, issued_at) is unique.
    assert max(Counter((r.series_key, r.interval_start, r.issued_at) for r in batch.rows).values()) == 1

    # UTC, half-open intervals at native resolution.
    for row in batch.rows:
        assert row.interval_start.tzinfo is UTC
        assert row.interval_end - row.interval_start == m.resolution_td
        assert row.issued_at is None or row.issued_at.utcoffset() == timedelta(0)

    # Nothing filled or coerced: one row per source value, value unchanged, nulls kept as null.
    source: dict[tuple[str, datetime], Any] = {}
    for r in raw:
        for e in entries:
            if all(r.get(k) == v for k, v in e.dimensions.items()):
                source[(series_key(m, m.zones[r[m.zone_field]], e), parse_source_utc(r[m.time_field_utc]))] = r[e.field]
    assert len(source) == len(batch.rows)
    for row in batch.rows:
        value = source[(row.series_key, row.interval_start)]
        assert row.value == (None if value is None else float(value))


def test_series_key_format(state: RepoState) -> None:
    batch = _parse(state, "DayAheadPrices", _result("DayAheadPrices", "normal_day"), *_day(date(2026, 10, 14)))
    assert "dk.eds.DayAheadPrices.dk1.DayAheadPriceEUR" in batch.series
    assert "de.zone.de_lu" in {s.entity_id for s in batch.series.values()}
    fc = _parse(state, "Forecasts_Hour", _result("Forecasts_Hour", "normal_day"), *_day(date(2026, 10, 14)))
    assert "dk.eds.Forecasts_Hour.dk2.ForecastDayAhead.wind_offshore" in fc.series


def test_day_ahead_publishes_the_manifest_zones(state: RepoState) -> None:
    zones = {r["PriceArea"] for r in _raw_records("DayAheadPrices", "normal_day")}  # recorded without a filter
    assert zones == set(_manifest(state, "DayAheadPrices").zones) == {"DK1", "DK2", "DE", "NO2", "SE3", "SE4"}


@pytest.mark.parametrize("dataset", ["ImbalancePrice", "Forecasts_Hour", "GenerationProdTypeExchange"])
def test_dk_only_datasets_publish_dk1_dk2(state: RepoState, dataset: str) -> None:
    zones = {r["PriceArea"] for r in _raw_records(dataset, "normal_day")}  # recorded without a filter
    assert zones == {"DK1", "DK2"} == set(_manifest(state, dataset).zones)


def test_elspotprices_zones_match_manifest(state: RepoState) -> None:
    zones = {r["PriceArea"] for r in _raw_records("Elspotprices", "resolution_switch")}  # recorded without a filter
    assert zones == set(_manifest(state, "Elspotprices").zones)


def test_resolution_switch_is_contiguous(state: RepoState) -> None:
    """Hourly Elspotprices ends exactly where 15-minute DayAheadPrices begins, 2025-09-30T22:00Z."""
    switch = datetime(2025, 9, 30, 22, tzinfo=UTC)
    two_days = (_day(date(2025, 9, 30))[0], _day(date(2025, 10, 1))[1])
    hourly = _parse(state, "Elspotprices", _result("Elspotprices", "resolution_switch"), *two_days)
    # The DayAheadPrices fixture was requested from 2025-09-30T12:00Z: nothing exists before the switch.
    quarter = _parse(state, "DayAheadPrices", _result("DayAheadPrices", "resolution_switch"), *two_days)
    for zone in ("dk1", "dk2"):
        h = [r for r in hourly.rows if f".{zone}." in r.series_key]
        q = [r for r in quarter.rows if f".{zone}." in r.series_key]
        assert len(h) == 24 and len(q) == 96
        assert max(r.interval_end for r in h) == switch == min(r.interval_start for r in q)
    assert min(parse_source_utc(r["TimeUTC"]) for r in _raw_records("DayAheadPrices", "resolution_switch")) == switch


def test_imbalance_explicit_nulls_stay_null(state: RepoState) -> None:
    result = _result("ImbalancePrice", "explicit_nulls")
    batch = _parse(state, "ImbalancePrice", result, *_day(date(2025, 3, 14)))
    nulls = [r for r in batch.rows if r.value is None]
    assert {r.series_key.split(".")[-1] for r in nulls} == {"SatisfiedDemand", "DominatingDirection"}
    first = datetime(2025, 3, 14, 13, 45, tzinfo=UTC)
    assert sorted({r.interval_start for r in nulls}) == [first + timedelta(minutes=15 * i) for i in range(5)]
    assert len(nulls) == 5 * 2 * 2  # five quarter-hours, two zones, two fields
    # No suspended (null) ImbalancePriceEUR exists at the source (every month checked on 2026-10-18).
    assert all(r.value is not None for r in batch.rows if r.series_key.endswith("ImbalancePriceEUR"))


def test_imbalance_history_starts_at_1115_utc(state: RepoState) -> None:
    batch = _parse(state, "ImbalancePrice", _result("ImbalancePrice", "history_start"), *_day(date(2025, 3, 4)))
    assert batch.series
    for key in batch.series:
        starts = sorted(r.interval_start for r in batch.rows if r.series_key == key)
        assert starts[0] == datetime(2025, 3, 4, 11, 15, tzinfo=UTC)
        assert len(starts) == 47  # of 96: the first partition is partial at the source


def test_afrr_down_is_published_non_positive(state: RepoState) -> None:
    batch = _parse(state, "ImbalancePrice", _result("ImbalancePrice", "normal_day"), *_day(date(2026, 10, 14)))
    down = [r.value for r in batch.rows if r.series_key.endswith("aFRRDownMW") and r.value is not None]
    assert down and max(down) <= 0 < -min(down)


def test_afrr_capacity_demand_is_null_before_2025_08_21(state: RepoState) -> None:
    early = _parse(
        state, "AfrrReservesNordic", _result("AfrrReservesNordic", "history_start"), *_day(date(2024, 10, 2))
    )
    demand = [r for r in early.rows if r.series_key.endswith(("UpDemandMW", "DownDemandMW"))]
    assert len(demand) == 24 * 2 * 2 and all(r.value is None for r in demand)
    assert all(r.value is not None for r in early.rows if r.series_key.endswith(("PriceEUR", "ProcuredMW")))
    later = _parse(state, "AfrrReservesNordic", _result("AfrrReservesNordic", "normal_day"), *_day(date(2026, 10, 14)))
    assert all(r.value is not None for r in later.rows)


def test_afrr_capacity_first_hour_and_sign(state: RepoState) -> None:
    m = _manifest(state, "AfrrReservesNordic")
    assert m.history_start_local == date(2024, 10, 2) and m.resolution_td == timedelta(hours=1)
    batch = _parse(
        state, "AfrrReservesNordic", _result("AfrrReservesNordic", "history_start"), *_day(date(2024, 10, 2))
    )
    assert min(r.interval_start for r in batch.rows) == datetime(2024, 10, 1, 22, tzinfo=UTC)
    down = [r.value for r in batch.rows if r.series_key.endswith("DownProcuredMW") and r.value is not None]
    assert down and min(down) >= 0  # published as a positive magnitude


def test_afrr_capacity_zones(state: RepoState) -> None:
    assert {r["PriceArea"] for r in _raw_records("AfrrReservesNordic", "normal_day")} == {"DK1", "DK2"}
    assert set(_manifest(state, "AfrrReservesNordic").zones) == {"DK1", "DK2"}


@pytest.mark.parametrize(("dataset", "day"), EMPTY_CASES, ids=[d for d, _ in EMPTY_CASES])
def test_empty_response(state: RepoState, dataset: str, day: date) -> None:
    m = _manifest(state, dataset)
    result = _result(dataset, "empty")
    assert _adapter().records(result) == []
    batch = _parse(state, dataset, result, *_day(day))
    assert batch.rows == [] and batch.series == {}
    # Inside the manifest's history an empty past day is a hard deviation (ADR 0013); outside it, no
    # rows are expected and nothing is flagged.
    expected = expected_partition_rows(m, TZ, day, m.records_per_interval_per_zone)
    fp = compute(
        status=200, content_type=result.content_type, size=len(result.content), records=[], rows_expected=expected
    )
    assert severity(compare(fp, Baseline())) == ("hard" if expected else "none")


def test_forecast_issue_times_across_dst(state: RepoState) -> None:
    m = _manifest(state, "Forecasts_Hour")
    for case, day, day_ahead, intraday in [
        ("dst_end", date(2025, 10, 26), datetime(2025, 10, 25, 16, tzinfo=UTC), datetime(2025, 10, 26, 5, tzinfo=UTC)),
        ("dst_start", date(2026, 3, 29), datetime(2026, 3, 28, 17, tzinfo=UTC), datetime(2026, 3, 29, 4, tzinfo=UTC)),
    ]:
        batch = _parse(state, "Forecasts_Hour", _result("Forecasts_Hour", case), *_day(day))
        assert len(batch.rows) == len(expected_intervals(day, TZ, m.resolution_td)) * 2 * len(m.entries)
        issued: dict[str, set[datetime | None]] = defaultdict(set)
        for r in batch.rows:
            issued[r.series_key.split(".")[4]].add(r.issued_at)
        assert issued["ForecastDayAhead"] == {day_ahead}  # 18:00 local the day before
        assert issued["ForecastIntraday"] == {intraday}  # 06:00 local the same day
        stamps = {parse_source_utc(r["TimestampUTC"]) for r in _raw_records("Forecasts_Hour", case)}
        assert issued["ForecastCurrent"] <= stamps  # TimestampUTC of each record


def test_forecast_vintage_not_issued_at_fetch_time_is_skipped(state: RepoState) -> None:
    """Fetched at 05:00 local, the response cannot hold that day's 06:00 intraday vintage yet."""
    fetched = datetime(2026, 10, 14, 3, tzinfo=UTC)
    result = _result("Forecasts_Hour", "normal_day", fetched_at=fetched)
    batch = _parse(state, "Forecasts_Hour", result, *_day(date(2026, 10, 14)))
    fields = Counter(r.series_key.split(".")[4] for r in batch.rows)
    assert fields["ForecastIntraday"] == 0
    assert fields["ForecastDayAhead"] == 24 * 2 * 3  # issued 2026-10-13 18:00 local
    assert all(r.issued_at is not None and r.issued_at <= fetched for r in batch.rows)


def test_lookahead_window_covers_next_day(state: RepoState) -> None:
    """D = 2026-10-17 and D+1, as a run on D after next-day publication sees them: partition D with
    lookahead P1D parses D+1 too, while the fingerprint records stay within D."""
    m = _manifest(state, "DayAheadPrices")
    day = date(2026, 10, 17)
    w = ingest_windows(m, TZ, day)
    assert (w.day_start, w.day_end) == _day(day)
    assert w.fetch_start == w.day_start - m.lookback_td
    assert w.fetch_end == _day(day + timedelta(days=1))[1]
    result = _result("DayAheadPrices", "lookahead")
    batch = _parse(state, "DayAheadPrices", result, w.fetch_start, w.fetch_end)
    tomorrow = _day(day + timedelta(days=1))
    assert sum(1 for r in batch.rows if tomorrow[0] <= r.interval_start < tomorrow[1]) == 96 * 2
    assert len(partition_records(m, _adapter().records(result), w.day_start, w.day_end)) == 96 * 2
    # Without lookahead (the old behaviour) tomorrow's prices would be dropped.
    old = _parse(state, "DayAheadPrices", result, w.fetch_start, w.day_end)
    assert max(r.interval_start for r in old.rows) < w.day_end


def test_soft_deviation_quarantines_only_the_partition_day(state: RepoState) -> None:
    """The real 911.14 EUR/MWh spike (DK2, 2026-02-17T16:45Z): the run for 2026-02-17 parses its
    lookback (2026-02-16) and lookahead (2026-02-18) too, but only the fingerprinted day is quarantined."""
    m = _manifest(state, "DayAheadPrices")
    day = date(2026, 2, 17)
    w = ingest_windows(m, TZ, day)
    rows = _parse(state, "DayAheadPrices", _result("DayAheadPrices", "spike"), w.fetch_start, w.fetch_end).rows
    marked = quarantine_partition_day(rows, w)
    assert [replace(r, quality="ok") for r in marked] == rows  # only the label changes
    inside = [r for r in marked if w.day_start <= r.interval_start < w.day_end]
    outside = [r for r in marked if not w.day_start <= r.interval_start < w.day_end]
    assert len(inside) == 96 * 2 and len(outside) == 2 * 96 * 2  # the fixture starts on 2026-02-16
    assert {r.quality for r in inside} == {"quarantined"} and {r.quality for r in outside} == {"ok"}
    spike = max(marked, key=lambda r: r.value or 0.0)
    assert (spike.value, spike.quality) == (911.140015, "quarantined")
    assert spike.interval_start == datetime(2026, 2, 17, 16, 45, tzinfo=UTC)
    edges = {r.interval_start: r.quality for r in marked}
    q = timedelta(minutes=15)
    assert [edges[t] for t in (w.day_start - q, w.day_start, w.day_end - q, w.day_end)] == [
        "ok",
        "quarantined",
        "quarantined",
        "ok",
    ]


@pytest.mark.parametrize(("day", "n"), [(date(2026, 3, 29), 92), (date(2026, 2, 17), 96), (date(2025, 10, 26), 100)])
def test_quarantined_partition_follows_local_day_length(state: RepoState, day: date, n: int) -> None:
    m = _manifest(state, "DayAheadPrices")
    w = ingest_windows(m, TZ, day)
    q = timedelta(minutes=15)
    count = int((w.fetch_end - w.fetch_start) / q)
    rows = [ObservationRow("k", w.fetch_start + i * q, w.fetch_start + (i + 1) * q, None, 1.0) for i in range(count)]
    marked = quarantine_partition_day(rows, w)
    assert sum(r.quality == "quarantined" for r in marked) == n
    assert sum(r.quality == "ok" for r in marked) == count - n


def test_non_numeric_value_is_a_parse_error(state: RepoState) -> None:
    record = _raw_records("DayAheadPrices", "normal_day")[0] | {"DayAheadPriceEUR": "12,5"}
    bad = FetchResult(
        url="x",
        params={},
        status=200,
        content_type="application/json",
        fetched_at=datetime(2026, 10, 18, tzinfo=UTC),
        content=json.dumps({"total": 1, "records": [record]}).encode(),
    )
    with pytest.raises(ParseError):
        _parse(state, "DayAheadPrices", bad, *_day(date(2026, 10, 14)))


def test_recent_window_starts_hours_before_the_tick(state: RepoState) -> None:
    """Frequent ticks fetch from recent_lookback before the tick (floored to the hour); runs without a tick
    time, and manifests without recent_lookback, keep the full lookback."""
    m = _manifest(state, "ImbalancePrice")
    day = date(2026, 10, 17)
    tick = datetime(2026, 10, 17, 9, 47, tzinfo=UTC)
    w = ingest_windows(m, TZ, day, tick)
    assert w.fetch_start == datetime(2026, 10, 17, 6, 0, tzinfo=UTC)
    assert (w.day_start, w.day_end, w.fetch_end) == (*_day(day), _day(day)[1])
    assert ingest_windows(m, TZ, day).fetch_start == w.day_start - timedelta(days=4)
    plain = m.model_copy(update={"recent_lookback": None})
    assert ingest_windows(plain, TZ, day, tick).fetch_start == w.day_start - timedelta(days=4)
    forecasts = ingest_windows(_manifest(state, "Forecasts_Hour"), TZ, day, tick)
    assert forecasts.fetch_end == _day(day + timedelta(days=1))[1]  # tomorrow stays inside the hourly window


def test_recent_lookback_is_validated(state: RepoState) -> None:
    m = _manifest(state, "ImbalancePrice")
    with pytest.raises(ValueError, match="must not exceed lookback"):
        Manifest(**{**m.model_dump(), "recent_lookback": "P5D"})
