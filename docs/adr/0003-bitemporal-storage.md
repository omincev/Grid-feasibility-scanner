# ADR 0003: Record valid time and knowledge time; never overwrite

Status: accepted, 2026-10-17.

## Context

Sources revise values. Forecasts are issued many times for the same target interval. Capacity maps and registers are replaced in place on the source website. Investors will ask why a site scored 72 in November, and backtests must not use information that was published after the decision they simulate.

## Decision

Observations carry `interval_start`, `interval_end` (valid time), `issued_at` (when the source produced a forecast; null for actuals) and `ingested_at` (when we learned it). Facts carry `valid_from`, `valid_to` and `known_at`. Writes append: a revised value becomes a new row with a newer `ingested_at`; an unchanged value is not written again. The view `core.observation_latest` serves the latest knowledge, and `core.observation_asof(timestamp)` serves what was known at a given moment.

Sources that only show their current state (capacity maps, queue maps, current forecasts) are captured on a schedule by the snapshot watcher from day one, because their history cannot be recovered later.

## Consequences

Every score, estimate and valuation can be reproduced for a past date. Forecast models can be trained on covariates as they were issued. Storage grows with revision frequency, which is small for our sources. Queries that want "the current value" must use the `_latest` view, never the base table.
