---
id: f2_curtailment
version: 1
title: F2 Curtailment proxy
status: provisional
owner: [oleg, michael]
inputs:
  - day_ahead_price
  - generation_forecast.solar
  - generation_forecast.wind_onshore
  - generation_forecast.wind_offshore
  - generation_actual.solar
  - generation_actual.wind_onshore
  - generation_actual.wind_offshore
parameters: []
outputs: [f2_nonpositive_price_share, f2_curtailment_proxy_mwh]
known_biases:
  - A proxy, not measured curtailment. Prices at or below zero also occur without curtailment.
  - Forecast minus actual production also measures forecast error, not only curtailed energy.
  - The MWh proxy needs forecasts and actuals, which exist for the Danish zones only; reference zones get the price share alone.
---

Specification: `docs/features.md`, "F2 Curtailment proxy". Code: `scanner.features.zone` (F2 functions), run by `scanner.features.run`.

**What it measures.** How often renewable output meets prices so low that producers would rather curtail.

**Formulas.**

- `f2_nonpositive_price_share`: the share of time (weighted by interval length) with a day-ahead price at or below 0 EUR/MWh, at native resolution. This is the statistic the scores read.
- `f2_curtailment_proxy_mwh`: per hour with a price at or below zero (price averaged to the hour), the day-ahead forecast of wind onshore, wind offshore and solar minus the actual production, counted only where positive, summed in MWh. The forecast is the latest vintage issued at or before the hour starts. Hours missing any input are not evaluated.

Both are written per local day (`daily_share`, `daily_sum`), for the window and for each regime sub-window. Label: "proxy".
