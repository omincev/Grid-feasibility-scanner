# BESS v0: effect of simultaneous charge and discharge (2026-10-18)

Question: the v0 LP (`src/scanner/valuation/bess.py`, spec in `docs/valuation.md`) lets a battery charge and
discharge in the same interval. With round-trip efficiency below 1 and negative prices that could "burn" energy
for revenue. How much does it overstate the v0 range? This is a measurement only. `bess.py` and
`docs/valuation.md` are unchanged; changing the model is an H7 decision.

## Method

- Prices: day-ahead prices of DK1 and DK2 in the main database (read-only session), window 2025-03-04 to
  2026-10-17 local, the prices the valuation run uses (`load_input`, native resolution, `complete_days`):
  593 valued days and 41,735 intervals per zone, of which 1,357 (DK1) and 681 (DK2) have a negative price.
- Battery: proposed `bess_mvp` values, as in the stored results: 1 MW, 2 h, round trip 0.91 (eta 0.9539 each
  way), SoC 0.1 to 0.9, start/end 0.5, 1 cycle per day.
- Current LP: `dispatch_day` as is.
- Exclusive variant: the same day LP as a MILP (`scipy.optimize.milp`, HiGHS) with a binary `u_t` per interval:
  `c_t <= P*u_t`, `d_t <= P*(1-u_t)`. Everything else identical.
- Ranges: `annual_range` over the 229 complete rolling 365-day windows. Simultaneous means charge and discharge
  both above 1e-6 MW in the same interval of the LP solution.
- Reproduce: `uv run python scripts/lp_exclusivity_study.py <database url>`.

## Results (EUR/MW/year)

| Zone | Variant | Low | High |
|---|---|---|---|
| DK1 | current LP | 57,565.60 | 72,230.59 |
| DK1 | exclusive MILP | 57,564.85 | 72,230.58 |
| DK1 | difference | 0.75 (0.0013 %) | 0.009 (0.00001 %) |
| DK2 | current LP | 63,238.91 | 76,306.77 |
| DK2 | exclusive MILP | 63,238.28 | 76,306.72 |
| DK2 | difference | 0.63 (0.0010 %) | 0.052 (0.00007 %) |

Simultaneous charge and discharge in the current solution: 2 intervals on 1 day (DK1) and 2 intervals on 2 days
(DK2), out of 41,735 intervals per zone. The MILP differs from the LP on 2 days (DK1) and 7 days (DK2); the
largest single-day difference is 0.75 EUR/MW (DK1) and 0.46 EUR/MW (DK2). Summed over the whole history the
difference is 0.76 EUR/MW (DK1) and 0.68 EUR/MW (DK2), against totals of 115,071 and 120,075.

Runtime (593 days per zone, one thread): LP 1.9 s (DK1) and 2.2 s (DK2); MILP 10.4 s and 11.2 s, about 5 times
slower.

## Reading

The overstatement is negligible for Denmark with these parameters: under 0.002 % and under 1 EUR/MW/yr on
about 57,000 to 76,000. The likely reason is that burning energy uses the same limits that bound ordinary
arbitrage (power, SoC window, one discharged cycle per day), and a negative price is rarely deep enough to pay
for the round-trip loss on top of displacing a useful cycle (not tested separately). Both ends of the range
move by a negligible amount, so the low/high spread is unchanged. The result holds for the proposed
parameters and day-ahead prices only; with a higher cycle limit, a lower efficiency or deeper negative prices
(for example intraday), re-run the script before relying on it.

If H7 wants the constraint anyway, the MILP above is the reference; the cost is about 5 times the solve time.
