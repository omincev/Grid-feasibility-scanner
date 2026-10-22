"""BESS v0: daily linear-programme dispatch on day-ahead prices (docs/valuation.md, "BESS v0").

Perfect foresight within each day, day-ahead arbitrage only. Results are an upper bound and must be
labelled as such wherever they are shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
from scipy.optimize import linprog


@dataclass(frozen=True)
class BessParams:
    power_mw: float
    energy_mwh: float
    charge_efficiency: float
    discharge_efficiency: float
    soc_min_fraction: float
    soc_max_fraction: float
    soc_initial_fraction: float
    max_cycles_per_day: float

    def __post_init__(self) -> None:
        if not (0 < self.charge_efficiency <= 1 and 0 < self.discharge_efficiency <= 1):
            raise ValueError("efficiencies must be in (0, 1]")
        if not (0 <= self.soc_min_fraction <= self.soc_initial_fraction <= self.soc_max_fraction <= 1):
            raise ValueError("need 0 <= soc_min <= soc_initial <= soc_max <= 1")
        if self.power_mw <= 0 or self.energy_mwh <= 0 or self.max_cycles_per_day <= 0:
            raise ValueError("power, energy and cycles must be positive")

    @classmethod
    def from_round_trip(
        cls, *, power_mw: float, duration_h: float, round_trip_efficiency: float, **kw: float
    ) -> BessParams:
        eta = round_trip_efficiency**0.5
        return cls(
            power_mw=power_mw,
            energy_mwh=power_mw * duration_h,
            charge_efficiency=eta,
            discharge_efficiency=eta,
            **kw,
        )


@dataclass(frozen=True)
class DispatchResult:
    revenue_eur: float
    charge_mw: np.ndarray
    discharge_mw: np.ndarray
    soc_mwh: np.ndarray


def dispatch_day(prices_eur_mwh: np.ndarray, dt_h: float, p: BessParams) -> DispatchResult:
    """Maximise sum(price * (discharge - charge) * dt) subject to SoC dynamics and limits.

    Variables x = [c_0..c_{T-1}, d_0..d_{T-1}, s_1..s_T]. s_0 = s_T = initial SoC.
    NaN prices are not allowed: the caller decides how to treat missing intervals (skip the day).
    """
    prices = np.asarray(prices_eur_mwh, dtype=float)
    if np.isnan(prices).any():
        raise ValueError("prices contain NaN; skip or split the day instead of filling")
    t = len(prices)
    n = 3 * t
    s_init = p.soc_initial_fraction * p.energy_mwh

    cost = np.concatenate([prices * dt_h, -prices * dt_h, np.zeros(t)])  # minimise -revenue

    a_eq = np.zeros((t + 1, n))
    b_eq = np.zeros(t + 1)
    for i in range(t):
        # s_i - s_{i-1} - eta_c * c_i * dt + d_i * dt / eta_d = 0
        a_eq[i, 2 * t + i] = 1.0
        if i > 0:
            a_eq[i, 2 * t + i - 1] = -1.0
        else:
            b_eq[i] = s_init
        a_eq[i, i] = -p.charge_efficiency * dt_h
        a_eq[i, t + i] = dt_h / p.discharge_efficiency
    a_eq[t, 3 * t - 1] = 1.0  # end SoC equals start SoC
    b_eq[t] = s_init

    a_ub = np.zeros((1, n))
    a_ub[0, t : 2 * t] = dt_h  # discharged energy within the cycle limit
    b_ub = np.array([p.max_cycles_per_day * p.energy_mwh])

    bounds = (
        [(0.0, p.power_mw)] * t
        + [(0.0, p.power_mw)] * t
        + [(p.soc_min_fraction * p.energy_mwh, p.soc_max_fraction * p.energy_mwh)] * t
    )
    res = linprog(cost, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if not res.success:
        raise RuntimeError(f"dispatch LP failed: {res.message}")
    x = res.x
    return DispatchResult(
        revenue_eur=float(-res.fun),
        charge_mw=x[:t],
        discharge_mw=x[t : 2 * t],
        soc_mwh=x[2 * t :],
    )


@dataclass(frozen=True)
class AnnualRange:
    low_eur_per_mw_year: float
    high_eur_per_mw_year: float
    windows: int
    days: int
    labels: tuple[str, ...]


def annual_range(daily_revenue: dict[date, float], power_mw: float) -> AnnualRange:
    """Low and high of rolling 365-day revenue sums per MW (docs/valuation.md).

    Days missing from the input are treated as missing, not zero: a window counts only if it has all
    365 days. With less than one complete window, returns the annualised mean as both ends and labels it:
    "less than one year of data" below 365 days, else "no complete 365-day window" (missing days break
    every window).
    """
    if not daily_revenue:
        raise ValueError("no daily revenue")
    days = sorted(daily_revenue)
    labels = ["zone-level", "perfect-foresight upper bound", "day-ahead arbitrage only"]
    by_day = daily_revenue
    sums: list[float] = []
    for i in range(len(days)):
        start = days[i]
        window = [date.fromordinal(start.toordinal() + k) for k in range(365)]
        if all(d in by_day for d in window):
            sums.append(sum(by_day[d] for d in window))
    if sums:
        return AnnualRange(min(sums) / power_mw, max(sums) / power_mw, len(sums), len(days), tuple(labels))
    mean_annual = sum(daily_revenue.values()) / len(days) * 365 / power_mw
    reason = "less than one year of data" if len(days) < 365 else "no complete 365-day window"
    return AnnualRange(mean_annual, mean_annual, 0, len(days), (*labels, reason))
