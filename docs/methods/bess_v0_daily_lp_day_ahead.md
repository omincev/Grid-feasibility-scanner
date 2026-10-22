---
id: bess_v0_daily_lp_day_ahead
version: 1
title: BESS return range v0 (daily LP on day-ahead prices)
status: provisional
owner: [oleg, misi]
inputs: [day_ahead_price]
parameters:
  - bess_template.power_mw
  - bess_template.duration_h
  - bess_template.soc_min_fraction
  - bess_template.soc_max_fraction
  - bess_template.soc_initial_fraction
  - bess_template.max_cycles_per_day
  - bess_template.round_trip_efficiency
  - bess_template.charge_efficiency
  - bess_template.discharge_efficiency
outputs: [val.run, val.result (revenue_eur_per_mw_year low and high), serving.return_ranges]
known_biases:
  - perfect foresight (overstates)
  - no balancing revenue (understates)
  - no degradation cost (overstates)
  - no saturation (overstates as more storage enters)
---

Specification: `docs/valuation.md`, "BESS v0". Code: `scanner.valuation.bess` (LP and ranges), `scanner.valuation.run` (per zone). Battery parameters come from the scenario's `bess_template` set. Logic changes need H7 sign-off.

**What it estimates.** What one battery could have earned per MW per year from day-ahead arbitrage alone in each zone, as a low and a high. Labelled "zone-level, perfect-foresight upper bound, day-ahead arbitrage only".

**Battery.** Power `power_mw` (1 MW, the normalisation unit), energy = power × `duration_h`. Charge and discharge efficiencies are `charge_efficiency` and `discharge_efficiency` when both are set, otherwise the square root of `round_trip_efficiency` each. State of charge between `soc_min_fraction` and `soc_max_fraction` of the energy, starting and ending each day at `soc_initial_fraction`; discharged energy per day at most `max_cycles_per_day` × energy.

**Per local day.** A linear programme over that day's prices at native resolution (hourly before 2025-10-01, 15-minute after) chooses charging and discharging to maximise revenue = Σ price × (discharge − charge) × interval length, knowing all prices in advance. Solved with `scipy.optimize.linprog` (HiGHS). A day is valued only if its prices cover the whole day with no missing or null interval; other days are skipped and counted, never filled.

**Range.** Daily revenues are summed over every window of 365 consecutive valued days; low and high are the smallest and largest window sums divided by power, in EUR/MW/year. With less than a year of data, or no complete window, low = high = the annualised mean, labelled "less than one year of data" or "no complete 365-day window".
