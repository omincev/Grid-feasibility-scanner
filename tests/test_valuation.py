from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from scanner.valuation.bess import BessParams, annual_range, dispatch_day
from scanner.valuation.financials import irr, lcos, npv, payback_years

IDEAL = BessParams(
    power_mw=1.0,
    energy_mwh=1.0,
    charge_efficiency=1.0,
    discharge_efficiency=1.0,
    soc_min_fraction=0.0,
    soc_max_fraction=1.0,
    soc_initial_fraction=0.0,
    max_cycles_per_day=1.0,
)


def test_lp_buys_low_sells_high() -> None:
    r = dispatch_day(np.array([0.0, 0.0, 100.0, 100.0]), dt_h=1.0, p=IDEAL)
    assert r.revenue_eur == pytest.approx(100.0)
    assert r.charge_mw[:2].sum() == pytest.approx(1.0)
    assert r.discharge_mw[2:].sum() == pytest.approx(1.0)


def test_lp_respects_order_of_time() -> None:
    # Expensive first, cheap later: with empty start and end SoC equal to start, nothing to earn.
    r = dispatch_day(np.array([100.0, 100.0, 0.0, 0.0]), dt_h=1.0, p=IDEAL)
    assert r.revenue_eur == pytest.approx(0.0, abs=1e-6)


def test_lp_efficiency_losses() -> None:
    p = BessParams.from_round_trip(
        power_mw=1.0,
        duration_h=1.0,
        round_trip_efficiency=0.81,
        soc_min_fraction=0.0,
        soc_max_fraction=1.0,
        soc_initial_fraction=0.0,
        max_cycles_per_day=1.0,
    )
    r = dispatch_day(np.array([0.0, 100.0]), dt_h=1.0, p=p)
    # Charging is power-limited to 1 MWh from the grid in the hour: 0.9 MWh stored, 0.81 MWh delivered.
    assert r.revenue_eur == pytest.approx(81.0, rel=1e-6)


def test_lp_rejects_nan() -> None:
    with pytest.raises(ValueError):
        dispatch_day(np.array([1.0, np.nan]), 1.0, IDEAL)


def test_annual_range_requires_complete_windows() -> None:
    start = date(2024, 1, 1)
    daily = {start + timedelta(days=i): 10.0 for i in range(800)}
    del daily[start + timedelta(days=100)]
    r = annual_range(daily, power_mw=1.0)
    # Complete windows start on days 101..435 (a missing day is never treated as zero).
    assert r.windows == 335
    assert r.low_eur_per_mw_year == r.high_eur_per_mw_year == pytest.approx(3650.0)
    short = annual_range({start + timedelta(days=i): 10.0 for i in range(30)}, power_mw=1.0)
    assert short.low_eur_per_mw_year == pytest.approx(3650.0)
    assert "less than one year of data" in short.labels


def test_financials() -> None:
    flows = [-100.0, 60.0, 60.0]
    assert npv(0.0, flows) == pytest.approx(20.0)
    rate = irr(flows)
    assert rate is not None and npv(rate, flows) == pytest.approx(0.0, abs=1e-6)
    assert payback_years([-100.0, 50.0, 50.0, 50.0]) == pytest.approx(2.0)
    assert payback_years([-100.0, 10.0]) is None
    assert lcos(0.0, [100.0, 0.0], [0.0, 10.0]) == pytest.approx(10.0)
