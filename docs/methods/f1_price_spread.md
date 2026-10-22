---
id: f1_price_spread
version: 1
title: F1 Price spread
status: provisional
owner: [oleg, michael]
inputs: [day_ahead_price]
parameters: []
outputs: [f1_intraday_spread_native, f1_intraday_spread_hourly, f1_interzone_spread]
known_biases:
  - Day-ahead prices only; intraday and balancing price spreads are not part of F1.
  - Native spreads widen at the 2025-10-01 switch to 15-minute prices (more intervals per day); only the hourly series is comparable across the switch.
  - A local day with any missing or null price is left out of the daily spreads, so days with publication gaps do not count.
---

Specification: `docs/features.md`, "F1 Price spread". Code: `scanner.features.zone` (F1 functions), run by `scanner.features.run`.

**What it measures.** How far the day-ahead price moves within a day, and how far two neighbouring zones' prices are apart.

**Input.** The day-ahead price of each zone, from the source its country file prefers (`precedence`): for Denmark `Elspotprices` (hourly) until 2025-09-30 and `DayAheadPrices` (15-minute) from 2025-10-01; for other zones ENTSO-E. Only rows at the resolution the market rules say is in force on that day are used. Null prices are dropped, never filled.

**Formulas.**

- `f1_intraday_spread_native`: for each local day (Europe/Copenhagen, 92, 96 or 100 quarter-hours or 23, 24 or 25 hours), the highest minus the lowest price, written only for days whose prices cover the whole day. Over the window: the mean and the 90th percentile of those daily spreads, and the number of days.
- `f1_intraday_spread_hourly`: the same after averaging the prices to whole hours (complete hours only). This is the series the scores read.
- `f1_interzone_spread`: per native interval, the absolute difference DK1 minus DK2, written to both zones. Over the window: the mean, and the share of intervals above 10 EUR/MWh (a fixed threshold of the specification, not a scenario parameter).

Every statistic is also written per local day (`daily_*`) and for each regime sub-window (split where the day-ahead resolution changes). Means over intervals are weighted by interval length.
