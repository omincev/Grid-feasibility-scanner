# ADR 0002: Store native resolution, resample only in features

Status: accepted, 2026-10-17.

## Context

The original build plan proposed one hourly table per zone. The Danish day-ahead price moved from hourly (`Elspotprices`, to 2025-09-30) to 15-minute (`DayAheadPrices`, from 2025-10-01). Imbalance data is 15-minute. Averaging to hourly erases the intra-hour swings that batteries earn on, which is exactly what feature 1 measures. Other sources will arrive at 5-minute, daily, monthly or irregular resolution.

## Decision

Each series is stored at the resolution the source publishes. `core.series.resolution` records it as an ISO 8601 duration. A series that changes resolution at a known date is modelled as two series joined in a view, with the switch recorded in `market_rules/`. Resampling happens only inside features and serving views, through `scanner.core.time.resample`, which uses the aggregation rule from the codebook: `sum` for energy quantities, `mean` for prices and average power, `last` or `first` for state values, `none` when resampling is not meaningful (the function raises).

## Consequences

Feature code always states its target resolution. Comparing a 2025 hourly spread with a 2026 quarter-hourly spread is a modelling choice made visible in the feature spec, never an accident of storage. Storage grows about fourfold for 15-minute series, which is negligible at our volumes.
