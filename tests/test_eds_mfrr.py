"""mFRR capacity, extra-auction and energy-activation datasets against real recorded responses.

Fixtures were recorded with scripts/record_fixture.py (see their .meta.json sidecars). Shared helpers come
from test_eds_parse; this file only adds what is specific to the three mFRR datasets.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta

import pytest
from test_eds_parse import _adapter, _day, _manifest, _parse, _raw_records, _result

from scanner.adapters.energidataservice.adapter import series_key
from scanner.core.time import expected_intervals, parse_source_utc
from scanner.runtime.context import RepoState

TZ = "Europe/Copenhagen"
CAPACITY = ["mFRRCapacityMarket", "MfrrCapacityMarketExtra"]
EAM = "MfrrEnergyActivationMarket"

# (dataset, case, local day, hours or quarter-hours per zone and field)
DAY_CASES = [
    ("mFRRCapacityMarket", "normal_day", date(2026, 10, 14), 24),
    ("mFRRCapacityMarket", "dst_end", date(2025, 10, 26), 25),
    ("mFRRCapacityMarket", "dst_start", date(2026, 3, 29), 23),
    ("MfrrCapacityMarketExtra", "normal_day", date(2026, 10, 14), 24),
    ("MfrrCapacityMarketExtra", "dst_end", date(2025, 10, 26), 25),
    ("MfrrCapacityMarketExtra", "dst_start", date(2026, 3, 29), 23),
    (EAM, "normal_day", date(2026, 10, 14), 96),
    (EAM, "dst_end", date(2025, 10, 26), 100),
    (EAM, "dst_start", date(2026, 3, 29), 92),
    (EAM, "history_start", date(2025, 3, 4), 96),
]


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
    zones = {r[m.zone_field] for r in raw}
    assert zones == {"DK1", "DK2"}
    assert set(batch.series) == {series_key(m, m.zones[z], e) for z in zones for e in entries}

    expected = [s for s, _ in expected_intervals(day, TZ, m.resolution_td)]
    assert len(expected) == n
    starts: dict[str, list[datetime]] = defaultdict(list)
    for row in batch.rows:
        starts[row.series_key].append(row.interval_start)
    for key in batch.series:
        assert sorted(starts[key]) == expected, key
    assert max(Counter((r.series_key, r.interval_start, r.issued_at) for r in batch.rows).values()) == 1
    for row in batch.rows:
        assert row.interval_start.tzinfo is UTC
        assert row.interval_end - row.interval_start == m.resolution_td

    # One row per source value, value unchanged, nulls kept as null (nothing filled or coerced).
    source = {
        (series_key(m, m.zones[r[m.zone_field]], e), parse_source_utc(r[m.time_field_utc])): r[e.field]
        for r in raw
        for e in entries
    }
    assert len(source) == len(batch.rows)
    for row in batch.rows:
        value = source[(row.series_key, row.interval_start)]
        assert row.value == (None if value is None else float(value))


@pytest.mark.parametrize("dataset", [*CAPACITY, EAM])
def test_published_zones_are_dk1_dk2(state: RepoState, dataset: str) -> None:
    zones = {r["PriceArea"] for r in _raw_records(dataset, "normal_day")}
    assert zones == {"DK1", "DK2"} == set(_manifest(state, dataset).zones)


def test_resolutions(state: RepoState) -> None:
    assert [_manifest(state, d).resolution for d in [*CAPACITY, EAM]] == ["PT1H", "PT1H", "PT15M"]


def test_dkk_columns_are_not_ingested(state: RepoState) -> None:
    for dataset in CAPACITY:
        fields = {e.field for e in state.entries_for(_manifest(state, dataset))}
        assert not {f for f in fields if f.endswith("DKK")}
        assert {"UpPriceEUR", "DownPriceEUR", "UpProcuredMW", "DownProcuredMW"} <= fields


def test_scheduled_activation_prices_equal_imbalance_mfrr_prices() -> None:
    """Why mFRRSAUpEUR and mFRRSADownEUR are not ingested: ImbalancePrice already carries them."""
    eam = {(r["TimeUTC"], r["PriceArea"]): r for r in _raw_records(EAM, "normal_day")}
    imb = {(r["TimeUTC"], r["PriceArea"]): r for r in _raw_records("ImbalancePrice", "normal_day")}
    assert len(eam) == len(imb) == 192
    for key, r in eam.items():
        assert r["mFRRSAUpEUR"] == imb[key]["mFRRMarginalPriceUpEUR"]
        assert r["mFRRSADownEUR"] == imb[key]["mFRRMarginalPriceDownEUR"]


def test_eam_sa_prices_are_not_in_the_manifest(state: RepoState) -> None:
    fields = {e.field for e in state.entries_for(_manifest(state, EAM))}
    assert not {"mFRRSAUpEUR", "mFRRSADownEUR"} & fields
    assert {"TotalmFRRUpMW", "TotalmFRRDownMW", "mFRRSAUpReqMW"} <= fields


def test_eam_nulls_stay_null_and_zeros_stay_zero(state: RepoState) -> None:
    batch = _parse(state, EAM, _result(EAM, "normal_day"), *_day(date(2026, 10, 14)))
    by_field: dict[str, list[float | None]] = defaultdict(list)
    for r in batch.rows:
        by_field[r.series_key.split(".")[-1]].append(r.value)
    assert any(v is None for v in by_field["mFRRSAUpReqMW"])  # no request in most intervals
    assert any(v is not None for v in by_field["mFRRSAUpReqMW"])
    assert all(v is None for v in by_field["mFRRDAUpMW"])  # direct activation never occurred
    assert all(v is not None for v in by_field["TotalmFRRUpMW"])
    assert 0.0 in by_field["TotalmFRRUpMW"]


def test_eam_history_starts_at_local_midnight(state: RepoState) -> None:
    batch = _parse(state, EAM, _result(EAM, "history_start"), *_day(date(2025, 3, 4)))
    first = datetime(2025, 3, 3, 23, tzinfo=UTC)
    for key in batch.series:
        starts = sorted(r.interval_start for r in batch.rows if r.series_key == key)
        assert starts[0] == first and len(starts) == 96
    assert min(parse_source_utc(r["TimeUTC"]) for r in _raw_records(EAM, "history_start")) == first


def test_capacity_history_start_nulls_gap_and_hourly(state: RepoState) -> None:
    """First record 2023-06-20T22:00Z; demand columns are null then; local day 2023-06-23 is missing at the source."""
    m = _manifest(state, "mFRRCapacityMarket")
    assert m.history_start_local == date(2023, 6, 21)
    first_day = _parse(state, m.dataset, _result(m.dataset, "history_start"), *_day(date(2023, 6, 21)))
    assert min(r.interval_start for r in first_day.rows) == datetime(2023, 6, 20, 22, tzinfo=UTC)
    demand = [r.value for r in first_day.rows if r.series_key.endswith("DemandMW")]
    assert demand and all(v is None for v in demand)
    prices = [r.value for r in first_day.rows if r.series_key.endswith("PriceEUR")]
    assert prices and all(v is not None for v in prices)
    missing = _parse(state, m.dataset, _result(m.dataset, "history_start"), *_day(date(2023, 6, 23)))
    assert missing.rows == []
    after = _parse(state, m.dataset, _result(m.dataset, "history_start"), *_day(date(2023, 6, 24)))
    assert len(after.rows) == 24 * 2 * 6  # 24 hours, two zones, six ingested fields


@pytest.mark.parametrize("dataset", [*CAPACITY, EAM])
def test_empty_response_before_history(state: RepoState, dataset: str) -> None:
    result = _result(dataset, "empty")
    assert _adapter().records(result) == []
    day = _manifest(state, dataset).history_start_local - timedelta(days=1)
    batch = _parse(state, dataset, result, *_day(day))
    assert batch.rows == [] and batch.series == {}
