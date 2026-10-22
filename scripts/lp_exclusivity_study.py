"""One-off study (2026-10-18): how much the BESS v0 LP gains from charging and discharging in the same
interval, against a MILP with a binary per interval that forbids it. Read-only on the database.

Usage: uv run python scripts/lp_exclusivity_study.py <database url>
Does not change scanner.valuation.bess; changing the model is an H7 decision.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from typing import Any

import numpy as np
import psycopg
from scipy.optimize import Bounds, LinearConstraint, milp

from scanner import REPO_ROOT
from scanner.core.registry import load_countries, load_market_rules
from scanner.features.inputs import analysis_window, load_input
from scanner.features.registry import DAY_AHEAD_PRICE
from scanner.valuation.bess import BessParams, annual_range, dispatch_day
from scanner.valuation.run import bess_params, complete_days

# The proposed values of assumptions/bess_mvp.yaml, as used for the stored results.
PROPOSED = {
    "power_mw": 1.0,
    "duration_h": 2.0,
    "round_trip_efficiency": 0.91,
    "soc_min_fraction": 0.1,
    "soc_max_fraction": 0.9,
    "soc_initial_fraction": 0.5,
    "max_cycles_per_day": 1.0,
}
TOL = 1e-6  # MW


def dispatch_day_exclusive(prices: np.ndarray, dt_h: float, p: BessParams) -> float:
    """The day LP of scanner.valuation.bess with a binary u_t per interval: charge only if u_t = 1,
    discharge only if u_t = 0. Variables [c, d, s, u]."""
    t = len(prices)
    n = 4 * t
    s_init = p.soc_initial_fraction * p.energy_mwh
    cost = np.concatenate([prices * dt_h, -prices * dt_h, np.zeros(2 * t)])
    a = np.zeros((t + 1, n))
    rhs = np.zeros(t + 1)
    for i in range(t):
        a[i, 2 * t + i] = 1.0
        if i > 0:
            a[i, 2 * t + i - 1] = -1.0
        else:
            rhs[i] = s_init
        a[i, i] = -p.charge_efficiency * dt_h
        a[i, t + i] = dt_h / p.discharge_efficiency
    a[t, 3 * t - 1] = 1.0
    rhs[t] = s_init
    cycles = np.zeros((1, n))
    cycles[0, t : 2 * t] = dt_h
    link_c = np.zeros((t, n))  # c_i - P u_i <= 0
    link_d = np.zeros((t, n))  # d_i + P u_i <= P
    for i in range(t):
        link_c[i, i], link_c[i, 3 * t + i] = 1.0, -p.power_mw
        link_d[i, t + i], link_d[i, 3 * t + i] = 1.0, p.power_mw
    cons = [
        LinearConstraint(a, rhs, rhs),
        LinearConstraint(cycles, -np.inf, p.max_cycles_per_day * p.energy_mwh),
        LinearConstraint(link_c, -np.inf, 0.0),
        LinearConstraint(link_d, -np.inf, p.power_mw),
    ]
    lb = np.concatenate([np.zeros(2 * t), np.full(t, p.soc_min_fraction * p.energy_mwh), np.zeros(t)])
    ub = np.concatenate([np.full(2 * t, p.power_mw), np.full(t, p.soc_max_fraction * p.energy_mwh), np.ones(t)])
    integrality = np.concatenate([np.zeros(3 * t), np.ones(t)])
    res = milp(cost, constraints=cons, bounds=Bounds(lb, ub), integrality=integrality)
    if not res.success:
        raise RuntimeError(res.message)
    return float(-res.fun)


def main(url: str) -> dict[str, Any]:
    countries = load_countries(REPO_ROOT)
    c = countries["dk"]
    rules = load_market_rules(REPO_ROOT).get("dk")
    precedence = {cc: o.precedence for cc, o in countries.items()}
    bess = bess_params(PROPOSED)
    start, end = analysis_window(c.timezone, None)
    out: dict[str, Any] = {"battery": asdict(bess), "window": [start.isoformat(), end.isoformat()]}
    with psycopg.connect(url, options="-c default_transaction_read_only=on") as conn:
        for zone in ("dk.zone.dk1", "dk.zone.dk2"):
            p = load_input(
                conn,
                (DAY_AHEAD_PRICE.equivalence_class,),
                DAY_AHEAD_PRICE,
                [zone],
                start,
                end,
                rules=rules,
                tz=c.timezone,
                precedence=precedence,
            )
            days = complete_days(p, c.timezone)
            lp: dict[Any, float] = {}
            ex: dict[Any, float] = {}
            simul_intervals = simul_days = intervals = neg = 0
            t_lp = t_milp = 0.0
            for d, (prices, dt) in sorted(days.items()):
                t0 = time.perf_counter()
                r = dispatch_day(prices, dt, bess)
                t_lp += time.perf_counter() - t0
                lp[d] = r.revenue_eur
                both = int(np.sum((r.charge_mw > TOL) & (r.discharge_mw > TOL)))
                simul_intervals += both
                simul_days += both > 0
                intervals += len(prices)
                neg += int(np.sum(prices < 0))
                t0 = time.perf_counter()
                ex[d] = dispatch_day_exclusive(prices, dt, bess)
                t_milp += time.perf_counter() - t0
            a, b = annual_range(lp, bess.power_mw), annual_range(ex, bess.power_mw)
            out[zone] = {
                "days": len(days),
                "intervals": intervals,
                "negative_intervals": neg,
                "lp": (a.low_eur_per_mw_year, a.high_eur_per_mw_year),
                "milp": (b.low_eur_per_mw_year, b.high_eur_per_mw_year),
                "windows": a.windows,
                "labels": a.labels,
                "simultaneous_intervals": simul_intervals,
                "simultaneous_days": simul_days,
                "total_lp": sum(lp.values()),
                "total_milp": sum(ex.values()),
                "lp_seconds": t_lp,
                "milp_seconds": t_milp,
                "max_day_gap": max(lp[d] - ex[d] for d in lp),
                "days_with_gap": sum(1 for d in lp if lp[d] - ex[d] > 1e-6),
            }
    return out


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1]), indent=2, default=str))
