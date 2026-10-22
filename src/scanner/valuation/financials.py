"""Cash-flow metrics on yearly cash flows. Year 0 is the investment year (usually negative)."""

from __future__ import annotations

from collections.abc import Sequence


def npv(rate: float, cash_flows: Sequence[float]) -> float:
    if rate <= -1:
        raise ValueError("rate must be greater than -100%")
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cash_flows))


def irr(cash_flows: Sequence[float], lo: float = -0.99, hi: float = 10.0, tol: float = 1e-10) -> float | None:
    """Internal rate of return by bisection. None if NPV does not change sign on [lo, hi]."""
    f_lo, f_hi = npv(lo, cash_flows), npv(hi, cash_flows)
    if f_lo == 0:
        return lo
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = npv(mid, cash_flows)
        if abs(f_mid) < tol or (hi - lo) / 2 < tol:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def payback_years(cash_flows: Sequence[float]) -> float | None:
    """Years until cumulative cash flow turns non-negative, interpolated within the year. None if never."""
    cumulative = 0.0
    for t, cf in enumerate(cash_flows):
        previous = cumulative
        cumulative += cf
        if cumulative >= 0 and t > 0:
            return (t - 1) + (-previous / cf if cf else 0.0)
    return None


def lcos(rate: float, costs: Sequence[float], discharged_mwh: Sequence[float]) -> float:
    """Levelised cost of storage: discounted costs over discounted discharged energy (EUR/MWh)."""
    energy = npv(rate, discharged_mwh)
    if energy <= 0:
        raise ValueError("discounted discharged energy must be positive")
    return npv(rate, costs) / energy
