from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
from hypothesis import given
from hypothesis import strategies as st

from scanner.core.time import (
    TimeConventionError,
    ensure_utc,
    expected_count,
    expected_intervals,
    format_duration,
    local_day,
    parse_duration,
    parse_source_utc,
    resample,
)

CPH = "Europe/Copenhagen"
Q = timedelta(minutes=15)


@pytest.mark.parametrize(
    ("day", "quarters", "hours"),
    [
        (date(2025, 10, 26), 100, 25),  # DST ends
        (date(2026, 3, 29), 92, 23),  # DST starts
        (date(2025, 11, 12), 96, 24),
    ],
)
def test_danish_day_lengths(day: date, quarters: int, hours: int) -> None:
    assert expected_count(day, CPH, Q) == quarters
    assert expected_count(day, CPH, timedelta(hours=1)) == hours


def test_local_day_starts_at_local_midnight() -> None:
    first, _ = expected_intervals(date(2025, 10, 1), CPH, Q)[0]
    assert first == datetime(2025, 9, 30, 22, 0, tzinfo=UTC)


@given(st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31)))
def test_intervals_contiguous_half_open(day: date) -> None:
    ivs = expected_intervals(day, CPH, Q)
    assert len(ivs) in (92, 96, 100)
    for (a0, a1), (b0, _) in itertools.pairwise(ivs):
        assert a1 == b0
        assert a1 - a0 == Q
    assert all(local_day(s, CPH) == day for s, _ in ivs)


@pytest.mark.parametrize("s", ["PT15M", "PT1H", "P1D", "P3D", "PT26H", "-PT5H", "P0D", "PT36H"])
def test_duration_round_trip(s: str) -> None:
    td = parse_duration(s)
    assert parse_duration(format_duration(td)) == td


@pytest.mark.parametrize("bad", ["", "P", "PT", "1H", "P1W", "PT1.5H"])
def test_duration_rejects(bad: str) -> None:
    with pytest.raises(TimeConventionError):
        parse_duration(bad)


def test_source_utc_parsing() -> None:
    assert parse_source_utc("2025-10-01T00:00:00") == datetime(2025, 10, 1, tzinfo=UTC)
    assert parse_source_utc("2025-10-01T00:00:00Z") == datetime(2025, 10, 1, tzinfo=UTC)
    with pytest.raises(TimeConventionError):
        parse_source_utc("2025-10-01T02:00:00+02:00")


def test_naive_rejected() -> None:
    with pytest.raises(TimeConventionError):
        ensure_utc(datetime(2025, 1, 1))  # noqa: DTZ001


def test_resample_mean_and_sum() -> None:
    start = datetime(2025, 10, 1, tzinfo=UTC)
    df = pl.DataFrame(
        {
            "zone": ["dk1"] * 4,
            "interval_start": [start + i * Q for i in range(4)],
            "interval_end": [start + (i + 1) * Q for i in range(4)],
            "value": [1.0, 2.0, 3.0, 6.0],
        }
    )
    m = resample(df, timedelta(hours=1), "mean")
    assert m["value"].to_list() == [3.0] and m["n"].to_list() == [4]
    assert resample(df, timedelta(hours=1), "sum")["value"].to_list() == [12.0]
    with pytest.raises(TimeConventionError):
        resample(df, timedelta(hours=1), "none")
    with pytest.raises(TimeConventionError):
        resample(df, timedelta(days=1), "mean")
