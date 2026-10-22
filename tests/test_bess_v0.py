"""Offline tests for the BESS v0 run: complete local days (DST, resolution switch, gaps, nulls), skipped days."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from scanner.core.time import local_day_bounds
from scanner.valuation.bess import BessParams, annual_range, dispatch_day
from scanner.valuation.run import complete_days, daily_revenue, day_ranges, window_days

CPH = "Europe/Copenhagen"
Q, H = timedelta(minutes=15), timedelta(hours=1)
IDEAL = BessParams(1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0)
UTC_DT = pl.Datetime("us", "UTC")


def _prices(day: date, step: timedelta, values: list[float | None]) -> pl.DataFrame:
    start = local_day_bounds(day, CPH)[0]
    return pl.DataFrame(
        {
            "entity_id": "dk.zone.dk1",
            "interval_start": [start + i * step for i in range(len(values))],
            "interval_end": [start + (i + 1) * step for i in range(len(values))],
            "value": values,
        },
        schema_overrides={"interval_start": UTC_DT, "interval_end": UTC_DT, "value": pl.Float64},
    )


def _ramp(n: int) -> list[float | None]:
    return [float(i) for i in range(n)]


def test_complete_days_across_dst_and_the_resolution_switch() -> None:
    frames = [
        _prices(date(2025, 3, 30), H, _ramp(23)),  # DST start: 23 hours
        _prices(date(2025, 9, 30), H, _ramp(24)),
        _prices(date(2025, 10, 1), Q, _ramp(96)),  # first quarter-hour day
        _prices(date(2025, 10, 26), Q, _ramp(100)),  # DST end: 100 quarter-hours
        _prices(date(2026, 3, 29), Q, _ramp(92)),  # DST start: 92 quarter-hours
    ]
    days = complete_days(pl.concat(frames), CPH)
    assert {d: (len(p), dt) for d, (p, dt) in days.items()} == {
        date(2025, 3, 30): (23, 1.0),
        date(2025, 9, 30): (24, 1.0),
        date(2025, 10, 1): (96, 0.25),
        date(2025, 10, 26): (100, 0.25),
        date(2026, 3, 29): (92, 0.25),
    }
    assert days[date(2025, 10, 26)][0].tolist() == _ramp(100)


def test_incomplete_days_are_skipped_not_filled() -> None:
    gap = _prices(date(2025, 10, 2), Q, _ramp(96)).filter(pl.col("interval_start") != pl.col("interval_start").min())
    frames = [
        _prices(date(2025, 10, 1), Q, [*_ramp(50), None, *_ramp(45)]),  # explicit null
        gap,  # first quarter-hour missing
        _prices(date(2025, 10, 3), Q, _ramp(95)),  # last quarter-hour missing
        _prices(date(2025, 10, 4), Q, _ramp(96)).filter(pl.col("value") != 40.0),  # a hole in the middle
        _prices(date(2025, 10, 5), Q, _ramp(96)),
    ]
    assert list(complete_days(pl.concat(frames), CPH)) == [date(2025, 10, 5)]


def test_window_days_and_ranges() -> None:
    start, end = local_day_bounds(date(2025, 3, 4), CPH)[0], local_day_bounds(date(2025, 3, 8), CPH)[0]
    assert window_days(start, end, CPH) == [date(2025, 3, d) for d in (4, 5, 6, 7)]
    days = [date(2025, 3, 4), date(2025, 3, 5), date(2025, 3, 7), date(2025, 3, 10), date(2025, 3, 11)]
    assert day_ranges(reversed(days)) == [
        ["2025-03-04", "2025-03-05"],
        ["2025-03-07", "2025-03-07"],
        ["2025-03-10", "2025-03-11"],
    ]
    assert day_ranges([]) == []


def test_annual_range_over_daily_revenue_with_a_skipped_day() -> None:
    first = date(2025, 1, 1)
    prices = np.array([0.0] * 12 + [100.0] * 12)
    days = {first + timedelta(days=i): (prices, 1.0) for i in range(400)}
    skipped = first + timedelta(days=20)
    del days[skipped]
    revenue = daily_revenue(days, IDEAL)
    assert revenue[first] == pytest.approx(dispatch_day(prices, 1.0, IDEAL).revenue_eur) == pytest.approx(100.0)
    r = annual_range(revenue, IDEAL.power_mw)
    # 400 days, one skipped on day 20: complete 365-day windows start on days 21..35.
    assert r.windows == 15 and r.days == 399
    assert r.low_eur_per_mw_year == r.high_eur_per_mw_year == pytest.approx(36500.0)
    assert "less than one year of data" not in r.labels
    # Under 365 days: annualised mean, labelled as short history.
    short = annual_range(daily_revenue(dict(list(days.items())[:300]), IDEAL), 1.0)
    assert short.windows == 0 and "less than one year of data" in short.labels
    # Over 365 days, but skipped days break every window: annualised mean, labelled for what happened.
    del days[first + timedelta(days=200)]
    broken = annual_range(daily_revenue(days, IDEAL), 1.0)
    assert (broken.windows, broken.days) == (0, 398)
    assert broken.labels[-1] == "no complete 365-day window"
    assert broken.low_eur_per_mw_year == pytest.approx(36500.0)


def test_complete_days_ignores_utc_date_boundaries() -> None:
    # Local day 2025-06-01 runs 2025-05-31 22:00 to 2025-06-01 22:00 UTC.
    p = _prices(date(2025, 6, 1), H, _ramp(24))
    assert p["interval_start"].min() == datetime.fromisoformat("2025-05-31T22:00:00+00:00")
    assert list(complete_days(p, CPH)) == [date(2025, 6, 1)]
